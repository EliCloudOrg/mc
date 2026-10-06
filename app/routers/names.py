"""用户自助端点：`/v1/names`、`/v1/me`（mc.md §6.2）。

身份与 scope 由 `CurrentIdentity` 依赖统一强制（所有 `/v1/*` 都要求含 `mc:whitelist`）。
"""

from __future__ import annotations

from fastapi import APIRouter, Query, Request, Response
from pydantic import BaseModel, ConfigDict, field_validator

from ..deps import CurrentIdentity, DbDep, SettingsDep, WhitelistDep, check_submit_rate_limit, client_ip
from ..whitelist import (
    add_name,
    dirty_reason,
    list_entries,
    note_reason,
    quota_state,
    remove_name,
)

router = APIRouter(prefix="/v1", tags=["names"])


class NameCreate(BaseModel):
    """提交体。未知字段直接 400（§10 第 9 条：不静默忽略）。"""

    model_config = ConfigDict(extra="forbid")

    name: str
    note: str

    @field_validator("name")
    @classmethod
    def _check_name(cls, value: str) -> str:
        reason = dirty_reason(value)
        if reason:
            raise ValueError(reason)
        return value

    @field_validator("note")
    @classmethod
    def _check_note(cls, value: str) -> str:
        reason = note_reason(value)
        if reason:
            raise ValueError(reason)
        return value


@router.post("/names", status_code=201)
def create_name(
    payload: NameCreate,
    request: Request,
    response: Response,
    identity: CurrentIdentity,
    settings: SettingsDep,
    db: DbDep,
    whitelist: WhitelistDep,
) -> dict[str, object]:
    """提交一个 MC 用户名。首次 201；**重复提交同名 200（幂等）**。"""
    ip = client_ip(request)
    check_submit_rate_limit(settings=settings, user_id=identity.sub, ip=ip)

    row, created = add_name(
        db,
        settings,
        whitelist,
        user_id=identity.sub,
        name=payload.name,
        note=payload.note,
        request_ip=ip,
        user_agent=request.headers.get("user-agent"),
    )
    if not created:
        response.status_code = 200  # 幂等分支（§6.2 第 4 步第一支）
    return row.to_public_dict()


@router.get("/names")
def list_my_names(
    identity: CurrentIdentity,
    settings: SettingsDep,
    db: DbDep,
    include_removed: bool = Query(False, description="带上已撤回的历史条目"),
) -> dict[str, object]:
    rows = list_entries(db, identity.sub, include_removed=include_removed)
    return {
        "sub": identity.sub,
        "quota": quota_state(db, settings, identity.sub),
        "names": [row.to_public_dict() for row in rows],
    }


@router.delete("/names/{entry_id}")
def delete_my_name(
    entry_id: str,
    request: Request,
    identity: CurrentIdentity,
    settings: SettingsDep,
    db: DbDep,
    whitelist: WhitelistDep,
) -> dict[str, object]:
    """撤回自己的条目（别人的条目一律 404，不泄露存在性）。"""
    ip = client_ip(request)
    check_submit_rate_limit(settings=settings, user_id=identity.sub, ip=ip)

    row, _changed = remove_name(
        db,
        whitelist,
        entry_id=entry_id,
        user_id=identity.sub,
        removed_by="self",
        request_ip=ip,
    )
    return row.to_public_dict()


@router.get("/me")
def me(identity: CurrentIdentity, settings: SettingsDep) -> dict[str, object]:
    """前端登录后自检：只回令牌里的 claim，**不查 SSO 数据库**。"""
    return {
        "sub": identity.sub,
        "username": identity.username,
        "issuer": settings.sso_issuer,
        "scope": identity.scope,
    }
