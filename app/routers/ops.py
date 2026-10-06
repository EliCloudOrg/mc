"""运维端点：`/healthz` 与 `/v1/admin/*`（mc-whitelist.md §5.7、§6.2、决策 T2）。

管理接口用 `ADMIN_TOKEN` 保护，**未配置时整组 403**（fail closed）。
CLI 侧同构的入口见 `app/cli.py`。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict
from sqlalchemy import func, select

from .. import __version__
from ..config import Settings
from ..deps import AdminAuth, DbDep, SettingsDep, WhitelistDep, client_ip, require_admin
from ..models import AuditLog, McName
from ..whitelist import health_snapshot, reconcile, remove_name

router = APIRouter(tags=["ops"])


@router.get("/healthz", include_in_schema=False)
def healthz(settings: SettingsDep, whitelist: WhitelistDep) -> JSONResponse:
    """健康检查（§5.7）：**必须包含 RCON 可达性**；不可达时返回 503 但进程继续跑。"""
    payload, ok = health_snapshot(whitelist)
    payload["sso_issuer"] = settings.sso_issuer
    payload["version"] = __version__
    return JSONResponse(status_code=200 if ok else 503, content=payload)


admin = APIRouter(prefix="/v1/admin", tags=["admin"], dependencies=[Depends(require_admin)])


class ReconcileRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    apply: bool = False


@admin.get("/names")
def admin_list_names(
    db: DbDep,
    status: str | None = Query(None, description="active / removed；不传 = 全部"),
    limit: int = Query(200, ge=1, le=1000),
    offset: int = Query(0, ge=0),
) -> dict[str, object]:
    """列出全部绑定（含 sub、备注全文、时间、UUID）。"""
    statement = select(McName)
    count_statement = select(func.count()).select_from(McName)
    if status:
        statement = statement.where(McName.status == status)
        count_statement = count_statement.where(McName.status == status)

    total = db.scalar(count_statement) or 0
    rows = db.scalars(statement.order_by(McName.created_at.desc(), McName.id.desc()).limit(limit).offset(offset)).all()
    return {
        "total": int(total),
        "limit": limit,
        "offset": offset,
        "names": [row.to_admin_dict() for row in rows],
    }


@admin.get("/audit")
def admin_audit(
    db: DbDep,
    user_id: str | None = Query(None),
    action: str | None = Query(None),
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
) -> dict[str, object]:
    """审计日志（分页，可按 sub / action 过滤）。"""
    statement = select(AuditLog)
    count_statement = select(func.count()).select_from(AuditLog)
    if user_id:
        statement = statement.where(AuditLog.user_id == user_id)
        count_statement = count_statement.where(AuditLog.user_id == user_id)
    if action:
        statement = statement.where(AuditLog.action == action)
        count_statement = count_statement.where(AuditLog.action == action)

    total = db.scalar(count_statement) or 0
    rows = db.scalars(
        statement.order_by(AuditLog.created_at.desc(), AuditLog.id.desc()).limit(limit).offset(offset)
    ).all()
    return {
        "total": int(total),
        "limit": limit,
        "offset": offset,
        "logs": [row.to_public_dict() for row in rows],
    }


@admin.delete("/names/{entry_id}")
def admin_delete_name(
    entry_id: str,
    request: Request,
    db: DbDep,
    whitelist: WhitelistDep,
) -> dict[str, object]:
    """管理员强制移除（`removed_by=admin`）。"""
    row, _changed = remove_name(
        db,
        whitelist,
        entry_id=entry_id,
        user_id=None,  # 管理员越过归属校验
        removed_by="admin",
        request_ip=client_ip(request),
    )
    return row.to_admin_dict()


@admin.post("/reconcile")
def admin_reconcile(payload: ReconcileRequest, db: DbDep, whitelist: WhitelistDep) -> dict[str, object]:
    """与 MC 的 `whitelist list` 对账；`apply=true` 时补回漂移（**绝不自动删**）。"""
    return reconcile(db, whitelist, apply=payload.apply)


__all__ = ["router", "admin", "AdminAuth"]
