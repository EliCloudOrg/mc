"""业务编排：名额 / 唯一性 / 幂等 / 回读校验 / 审计。

规格：`docs/mc-whitelist.md` §6.2（处理顺序）、§7（并发与一致性）。两条铁律：

1. 写操作固定三步，且在**同一把锁内**完成：`list`（前置真源）→ `add`/`remove` → `list`（回读确认）；
2. **回执文本不参与正确性判断**（§5.3），只进日志与审计；回读没确认就失败，**绝不落库**。
   ——"库里说加了、白名单里没有"比"这次操作失败"糟糕得多。
"""

from __future__ import annotations

import logging
import re
import secrets
import time

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session as DbSession

from .config import Settings
from .errors import api_error, rcon_unavailable
from .models import (
    ACTION_ADD,
    ACTION_DENY,
    ACTION_RECONCILE,
    ACTION_REMOVE,
    RESULT_CONFLICT,
    RESULT_IDEMPOTENT,
    RESULT_NOT_FOUND,
    RESULT_OK,
    RESULT_QUOTA_EXCEEDED,
    RESULT_RCON_ERROR,
    STATUS_ACTIVE,
    STATUS_REMOVED,
    AuditLog,
    McName,
    WhitelistCache,
    dumps_detail,
    to_iso,
    utcnow,
)
from .rcon import RconError, WhitelistRcon, command_lock

logger = logging.getLogger(__name__)

# MC 官方用户名规则（§5.4）：先过正则，才允许进入 RCON 命令
NAME_RE = re.compile(r"\A[A-Za-z0-9_]{3,16}\Z")
NOTE_MIN = 1
NOTE_MAX = 200


# ------------------------------------------------------------------ 校验


def normalize_name(raw: str) -> str:
    """服务内部一律小写做键（§5.2 结论 3：服务端也把小写作为真值）。"""
    return raw.strip().lower()


def dirty_reason(raw: str | None) -> str | None:
    """返回不合格原因；None 表示合格。"""
    if raw is None:
        return "缺少 name"
    if not NAME_RE.match(raw):
        return "name 必须匹配 ^[A-Za-z0-9_]{3,16}$（MC 用户名规则）"
    return None


def note_reason(raw: str | None) -> str | None:
    if raw is None:
        return "缺少 note（备注必填，用于事后追责）"
    length = len(raw)
    if length < NOTE_MIN or length > NOTE_MAX:
        return f"note 长度必须在 {NOTE_MIN}–{NOTE_MAX} 之间"
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in raw):
        return "note 不能包含控制字符"
    return None


# ------------------------------------------------------------------ 审计


def new_audit_id() -> str:
    return f"au_{secrets.token_hex(8)}"


def _next_name_id(db: DbSession) -> str:
    """生成 mn_0001 形式的递增 ID；并发下靠重试 + 主键冲突兜底（与 SSO 同手法）。"""
    existing = db.scalars(select(McName.id)).all()
    max_index = 0
    for value in existing:
        if value.startswith("mn_"):
            suffix = value[3:]
            if suffix.isdigit():
                max_index = max(max_index, int(suffix))
    return f"mn_{max_index + 1:04d}"


def audit(
    db: DbSession,
    *,
    user_id: str | None,
    action: str,
    target: str | None,
    result: str,
    detail: dict[str, object] | None = None,
    request_ip: str | None = None,
) -> AuditLog:
    """写一条审计（调用方负责 commit）。**detail 里绝不放令牌原文**。"""
    row = AuditLog(
        id=new_audit_id(),
        user_id=user_id,
        action=action,
        target=target,
        result=result,
        detail=dumps_detail(detail),
        request_ip=request_ip,
        created_at=to_iso(utcnow()),
    )
    db.add(row)
    return row


def _record_failure(
    db: DbSession,
    *,
    user_id: str | None,
    action: str,
    target: str | None,
    result: str,
    detail: dict[str, object] | None = None,
    request_ip: str | None = None,
) -> None:
    """失败路径：先把审计落库再抛（§10 第 10 条：失败也要留痕）。"""
    audit(
        db,
        user_id=user_id,
        action=action,
        target=target,
        result=result,
        detail=detail,
        request_ip=request_ip,
    )
    db.commit()


# ------------------------------------------------------------------ 读取


def quota_state(db: DbSession, settings: Settings, user_id: str) -> dict[str, int]:
    used = db.scalar(
        select(func.count())
        .select_from(McName)
        .where(McName.user_id == user_id, McName.status == STATUS_ACTIVE)
    ) or 0
    limit = settings.max_names_per_user
    return {"limit": limit, "used": int(used), "remaining": max(limit - int(used), 0)}


def list_entries(db: DbSession, user_id: str, *, include_removed: bool = False) -> list[McName]:
    statement = select(McName).where(McName.user_id == user_id)
    if not include_removed:
        statement = statement.where(McName.status == STATUS_ACTIVE)
    return list(db.scalars(statement.order_by(McName.created_at.desc(), McName.id.desc())).all())


def active_row_for_name(db: DbSession, name: str) -> McName | None:
    return db.scalar(select(McName).where(McName.name == name, McName.status == STATUS_ACTIVE))


def all_active_rows(db: DbSession) -> list[McName]:
    return list(db.scalars(select(McName).where(McName.status == STATUS_ACTIVE)).all())


def sync_cache(db: DbSession, names: list[str]) -> None:
    """把刚读到的白名单快照写进 `whitelist_cache`（辅助对账；真源仍是 MC 的 list）。"""
    db.execute(delete(WhitelistCache))
    moment = to_iso(utcnow())
    for name in sorted(set(names)):
        db.add(WhitelistCache(name=name, uuid=None, synced_at=moment))


# ------------------------------------------------------------------ 写入


def add_name(
    db: DbSession,
    settings: Settings,
    whitelist: WhitelistRcon,
    *,
    user_id: str,
    name: str,
    note: str,
    request_ip: str | None,
    user_agent: str | None,
) -> tuple[McName, bool]:
    """提交一个 MC 用户名。返回 ``(条目, 是否新建)``；重复提交同名返回 ``(已有条目, False)``。

    处理顺序严格按 §6.2：库 → 白名单（真源）→ 名额 → add → 回读 → 落库。
    """
    normalized = normalize_name(name)
    display = name

    with command_lock:
        # 1) 库里已有 active 绑定？
        existing = active_row_for_name(db, normalized)
        if existing is not None:
            if existing.user_id == user_id:
                _record_failure(
                    db,
                    user_id=user_id,
                    action=ACTION_ADD,
                    target=normalized,
                    result=RESULT_IDEMPOTENT,
                    detail={"reason": "same sub already bound", "entry_id": existing.id},
                    request_ip=request_ip,
                )
                return existing, False
            _record_failure(
                db,
                user_id=user_id,
                action=ACTION_ADD,
                target=normalized,
                result=RESULT_CONFLICT,
                detail={"reason": "bound by another sub"},
                request_ip=request_ip,
            )
            raise api_error(409, "name_taken", f"用户名 {normalized} 已被其它账号绑定")

        # 2) 白名单真源（同时充当 §6.2 第 6 步的前置确认）
        try:
            current = whitelist.list_names()
        except RconError as exc:
            _record_failure(
                db,
                user_id=user_id,
                action=ACTION_ADD,
                target=normalized,
                result=RESULT_RCON_ERROR,
                detail={"stage": "precheck", "error": str(exc)},
                request_ip=request_ip,
            )
            raise rcon_unavailable(f"无法读取 MC 白名单：{exc}") from exc

        if normalized in current:
            _record_failure(
                db,
                user_id=user_id,
                action=ACTION_ADD,
                target=normalized,
                result=RESULT_CONFLICT,
                detail={"reason": "present in whitelist but not in db", "stage": "precheck"},
                request_ip=request_ip,
            )
            raise api_error(
                409,
                "name_taken",
                f"用户名 {normalized} 已在服务器白名单中（可能是管理员手工添加），请联系管理员",
            )

        # 3) 名额
        quota = quota_state(db, settings, user_id)
        if quota["used"] >= quota["limit"]:
            _record_failure(
                db,
                user_id=user_id,
                action=ACTION_ADD,
                target=normalized,
                result=RESULT_QUOTA_EXCEEDED,
                detail={"limit": quota["limit"], "used": quota["used"]},
                request_ip=request_ip,
            )
            raise api_error(
                409,
                "quota_exceeded",
                f"每个账号最多绑定 {quota['limit']} 个 MC 用户名，请先撤回一个",
            )

        # 4) 写入 + 回读确认
        reply = ""
        try:
            reply = whitelist.add(normalized)
            after = whitelist.list_names()
        except RconError as exc:
            _record_failure(
                db,
                user_id=user_id,
                action=ACTION_ADD,
                target=normalized,
                result=RESULT_RCON_ERROR,
                detail={"stage": "apply", "reply": reply, "error": str(exc)},
                request_ip=request_ip,
            )
            raise rcon_unavailable(f"写入白名单失败：{exc}") from exc

        if normalized not in after:
            # 回执说成功也不算数（§5.3）；此处**不落库**
            _record_failure(
                db,
                user_id=user_id,
                action=ACTION_ADD,
                target=normalized,
                result=RESULT_RCON_ERROR,
                detail={"stage": "verify", "reply": reply, "whitelist": after},
                request_ip=request_ip,
            )
            raise rcon_unavailable("白名单写入未被回读确认，本次未记录（请稍后重试或联系管理员）")

        # 5) 落库 + 审计
        row = McName(
            id=_next_name_id(db),
            user_id=user_id,
            name=normalized,
            name_display=display,
            note=note,
            status=STATUS_ACTIVE,
            uuid=None,  # UUID 由服务端决定且形态不稳定（§5.2 结论 4）→ 不构造、不推断
            request_ip=request_ip,
            user_agent=(user_agent or "")[:256] or None,
            created_at=to_iso(utcnow()),
        )
        db.add(row)
        sync_cache(db, after)
        audit(
            db,
            user_id=user_id,
            action=ACTION_ADD,
            target=normalized,
            result=RESULT_OK,
            detail={"reply": reply, "whitelist_size": len(after), "entry_id": row.id},
            request_ip=request_ip,
        )
        db.commit()
        logger.info("mc add sub=%s name=%s entry=%s", user_id, normalized, row.id)
        return row, True


def remove_name(
    db: DbSession,
    whitelist: WhitelistRcon,
    *,
    entry_id: str,
    user_id: str,
    removed_by: str,
    request_ip: str | None = None,
) -> tuple[McName, bool]:
    """撤回条目。``user_id`` 为 None 表示管理员操作（可越过归属校验）。"""
    with command_lock:
        row = db.get(McName, entry_id)
        if row is None or (user_id is not None and row.user_id != user_id):
            # 不属于自己一律 404：不泄露他人资源是否存在（§10 第 2 条）
            _record_failure(
                db,
                user_id=user_id,
                action=ACTION_REMOVE,
                target=entry_id,
                result=RESULT_NOT_FOUND,
                detail=None,
                request_ip=request_ip,
            )
            raise api_error(404, "not_found", "条目不存在")

        if row.status == STATUS_REMOVED:
            _record_failure(
                db,
                user_id=row.user_id,
                action=ACTION_REMOVE,
                target=row.name,
                result=RESULT_IDEMPOTENT,
                detail={"entry_id": row.id, "reason": "already removed"},
                request_ip=request_ip,
            )
            return row, False

        reply = ""
        try:
            reply = whitelist.remove(row.name)
            after = whitelist.list_names()
        except RconError as exc:
            _record_failure(
                db,
                user_id=row.user_id,
                action=ACTION_REMOVE,
                target=row.name,
                result=RESULT_RCON_ERROR,
                detail={"stage": "apply", "reply": reply, "error": str(exc)},
                request_ip=request_ip,
            )
            raise rcon_unavailable(f"撤回失败：{exc}") from exc

        if row.name in after:
            # 宁可状态不动，也不要出现「库里已移除、白名单里还在」（§7）
            _record_failure(
                db,
                user_id=row.user_id,
                action=ACTION_REMOVE,
                target=row.name,
                result=RESULT_RCON_ERROR,
                detail={"stage": "verify", "reply": reply, "whitelist": after},
                request_ip=request_ip,
            )
            raise rcon_unavailable("撤回未被回读确认，库状态未改动（请稍后重试）")

        row.status = STATUS_REMOVED
        row.removed_at = to_iso(utcnow())
        row.removed_by = removed_by
        sync_cache(db, after)
        audit(
            db,
            user_id=row.user_id,
            action=ACTION_REMOVE,
            target=row.name,
            result=RESULT_OK,
            detail={"reply": reply, "removed_by": removed_by, "entry_id": row.id},
            request_ip=request_ip,
        )
        db.commit()
        logger.info("mc remove sub=%s name=%s by=%s", row.user_id, row.name, removed_by)
        return row, True


# ------------------------------------------------------------------ 对账


def reconcile(db: DbSession, whitelist: WhitelistRcon, *, apply: bool = False, user_id: str | None = None) -> dict:
    """用 `whitelist list` 与库对账（§6.2 管理端点、§7、§11）。

    * 库 active 但白名单没有 → 漂移；``apply=true`` 时用 RCON 补回（恢复用户已获批的意图）；
    * 白名单有但库里没有 → **只报告，绝不自动删除**（可能是别人手工加的）。
    """
    with command_lock:
        try:
            current = whitelist.list_names()
        except RconError as exc:
            _record_failure(
                db,
                user_id=user_id,
                action=ACTION_RECONCILE,
                target=None,
                result=RESULT_RCON_ERROR,
                detail={"stage": "list", "error": str(exc)},
            )
            raise rcon_unavailable(f"无法读取 MC 白名单：{exc}") from exc

        by_name = {row.name: row for row in all_active_rows(db)}
        missing = sorted(set(by_name) - set(current))
        extra = sorted(set(current) - set(by_name))

        applied: list[str] = []
        failed: list[dict[str, str]] = []
        if apply:
            for name in missing:
                try:
                    whitelist.add(name)
                    after = whitelist.list_names()
                except RconError as exc:
                    failed.append({"name": name, "error": str(exc)})
                    continue
                if name in after:
                    applied.append(name)
                else:
                    failed.append({"name": name, "error": "回读未确认"})
            if applied:
                sync_cache(db, whitelist.list_names())

        report = {
            "whitelist": sorted(current),
            "db_active": sorted(by_name),
            "missing": missing,  # 库有、白名单无
            "extra": extra,  # 白名单有、库无（不自动删）
            "applied": applied,
            "failed": failed,
            "apply": bool(apply),
        }
        audit(
            db,
            user_id=user_id,
            action=ACTION_RECONCILE,
            target=None,
            result=RESULT_OK,
            detail={key: report[key] for key in ("missing", "extra", "applied", "failed", "apply")},
        )
        db.commit()
        return report


# ------------------------------------------------------------------ 健康


def health_snapshot(whitelist: WhitelistRcon) -> tuple[dict[str, object], bool]:
    """`GET /healthz` 的内容（§5.7）：**必须包含 RCON 可达性**，否则"容器在跑但 RCON 挂了"看不出来。"""
    started = time.perf_counter()
    try:
        names = whitelist.ping()
    except RconError as exc:
        return (
            {
                "status": "degraded",
                "rcon": {"reachable": False, "endpoint": whitelist.endpoint, "error": str(exc)},
                "whitelist_count": None,
            },
            False,
        )
    latency_ms = int(round((time.perf_counter() - started) * 1000))
    return (
        {
            "status": "ok",
            "rcon": {"reachable": True, "endpoint": whitelist.endpoint, "latency_ms": latency_ms},
            "whitelist_count": len(names),
        },
        True,
    )


def deny_log(
    db: DbSession,
    *,
    user_id: str | None,
    action: str,
    target: str | None,
    result: str,
    detail: dict[str, object] | None,
    request_ip: str | None,
) -> None:
    """被拒绝的请求也留痕（§10 第 10 条）。"""
    _record_failure(
        db,
        user_id=user_id,
        action=action or ACTION_DENY,
        target=target,
        result=result,
        detail=detail,
        request_ip=request_ip,
    )
