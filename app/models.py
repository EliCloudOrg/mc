"""表结构（SQLite，TEXT + ISO8601，与 SSO 同风格）。

对应 mc-whitelist.md §4：

* ``mc_names``        —— 用户名 ↔ SSO 账号 的绑定（一行 = 一次申请）
* ``audit_logs``      —— 全量审计（成功与失败都留痕）
* ``whitelist_cache`` —— 白名单快照（辅助对账；**真源永远是 MC 的 `whitelist list`**）

「全局唯一」在数据库层用**部分唯一索引**兜底：同一个 MC 用户名只允许一条 `active`
（大小写已归一为小写）。并发下由它保证不会写进两条 active，应用层据此返回 409 而不是 500。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from sqlalchemy import Index, Text, text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

ISO_FORMAT = "%Y-%m-%dT%H:%M:%SZ"

STATUS_ACTIVE = "active"
STATUS_REMOVED = "removed"

# 审计动作与结果（写库前统一取值，避免各处拼字符串拼出不一致）
ACTION_ADD = "add"
ACTION_REMOVE = "remove"
ACTION_RECONCILE = "reconcile"
ACTION_DENY = "deny"

RESULT_OK = "ok"
RESULT_IDEMPOTENT = "idempotent"
RESULT_CONFLICT = "conflict"
RESULT_QUOTA_EXCEEDED = "quota_exceeded"
RESULT_RCON_ERROR = "rcon_error"
RESULT_UNAUTHORIZED = "unauthorized"
RESULT_FORBIDDEN = "forbidden"
RESULT_NOT_FOUND = "not_found"
RESULT_RATE_LIMITED = "rate_limited"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def to_iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime(ISO_FORMAT)


def parse_iso(value: str) -> datetime:
    return datetime.strptime(value, ISO_FORMAT).replace(tzinfo=timezone.utc)


def dumps_detail(payload: dict[str, object] | None) -> str | None:
    if payload is None:
        return None
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


class Base(DeclarativeBase):
    pass


class McName(Base):
    __tablename__ = "mc_names"
    __table_args__ = (
        # 全局唯一：同一个 MC 用户名只能有一条 active（大小写已归一）
        Index(
            "ux_mc_names_active_name",
            "name",
            unique=True,
            sqlite_where=text("status = 'active'"),
            postgresql_where=text("status = 'active'"),
        ),
        # 名额统计（COUNT(*) WHERE user_id=? AND status='active'）走这个索引
        Index("ix_mc_names_user_status", "user_id", "status"),
    )

    id: Mapped[str] = mapped_column(Text, primary_key=True)  # 如 mn_0001
    user_id: Mapped[str] = mapped_column(Text, nullable=False)  # SSO 的 sub（不建外键：不跨库）
    name: Mapped[str] = mapped_column(Text, nullable=False)  # 归一后的 MC 用户名（小写）
    name_display: Mapped[str] = mapped_column(Text, nullable=False)  # 用户提交的原始写法，仅展示
    note: Mapped[str] = mapped_column(Text, nullable=False)  # 必填备注
    status: Mapped[str] = mapped_column(Text, nullable=False, default=STATUS_ACTIVE)
    uuid: Mapped[str | None] = mapped_column(Text, nullable=True)  # 服务端写入的 UUID（只记录，不构造）
    request_ip: Mapped[str | None] = mapped_column(Text, nullable=True)
    user_agent: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)
    removed_at: Mapped[str | None] = mapped_column(Text, nullable=True)
    removed_by: Mapped[str | None] = mapped_column(Text, nullable=True)  # self / admin / reconcile

    def to_public_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "name": self.name,
            "name_display": self.name_display,
            "note": self.note,
            "status": self.status,
            "uuid": self.uuid,
            "created_at": self.created_at,
            "removed_at": self.removed_at,
        }

    def to_admin_dict(self) -> dict[str, object]:
        """管理接口用：多带 sub、来源 IP/UA、撤销信息。"""
        payload = self.to_public_dict()
        payload.update(
            {
                "sub": self.user_id,
                "request_ip": self.request_ip,
                "user_agent": self.user_agent,
                "removed_by": self.removed_by,
            }
        )
        return payload


class AuditLog(Base):
    __tablename__ = "audit_logs"
    __table_args__ = (
        Index("ix_audit_logs_created_at", "created_at"),
        Index("ix_audit_logs_user_id", "user_id"),
    )

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    user_id: Mapped[str | None] = mapped_column(Text, nullable=True)  # 失败且未认证时为空
    action: Mapped[str] = mapped_column(Text, nullable=False)
    target: Mapped[str | None] = mapped_column(Text, nullable=True)
    result: Mapped[str] = mapped_column(Text, nullable=False)
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON：RCON 原文、错误原因（不含令牌）
    request_ip: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)

    def to_public_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "sub": self.user_id,
            "action": self.action,
            "target": self.target,
            "result": self.result,
            "detail": json.loads(self.detail) if self.detail else None,
            "request_ip": self.request_ip,
            "created_at": self.created_at,
        }


class WhitelistCache(Base):
    """白名单快照（辅助对账）。真源是 MC 的 `whitelist list`，本表只用来做漂移提示。"""

    __tablename__ = "whitelist_cache"

    name: Mapped[str] = mapped_column(Text, primary_key=True)  # 小写
    uuid: Mapped[str | None] = mapped_column(Text, nullable=True)
    synced_at: Mapped[str] = mapped_column(Text, nullable=False)
