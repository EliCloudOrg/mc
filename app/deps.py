"""FastAPI 依赖：配置、数据库、RCON、Bearer 身份、管理员鉴权、客户端 IP、限流。

与 `sso/app/deps.py` 同构；差别在于本服务的身份来自**验签**（不是自己签发），
以及所有 `/v1/*` 都要过 `REQUIRED_SCOPE`（决策 T1）。
"""

from __future__ import annotations

import logging
import secrets
from dataclasses import dataclass
from typing import Annotated, Any

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session as DbSession
from starlette.exceptions import HTTPException as StarletteHTTPException

from .config import Settings, get_settings
from .db import get_db
from .errors import api_error, limiter
from .models import ACTION_DENY
from .rcon import RconClient, WhitelistRcon
from .sso_auth import decode_access_token, get_jwks_cache, require_scope
from .whitelist import deny_log

logger = logging.getLogger(__name__)

ADMIN_AUTH_ATTEMPTS = 20
ADMIN_AUTH_WINDOW_SECONDS = 300

# 拒绝审计的写入限流：坏令牌刷审计表的成本必须可控
DENY_AUDIT_ATTEMPTS = 60
DENY_AUDIT_WINDOW_SECONDS = 300

bearer_scheme = HTTPBearer(auto_error=False, description="SSO 签发的 RS256 Access Token")

SettingsDep = Annotated[Settings, Depends(get_settings)]
DbDep = Annotated[DbSession, Depends(get_db)]


@dataclass(frozen=True)
class Identity:
    """已验签的调用方身份（只取令牌里的 claim，**不查 SSO 数据库**）。"""

    sub: str
    username: str | None
    claims: dict[str, Any]

    @property
    def scope(self) -> str:
        return str(self.claims.get("scope") or "")


def client_ip(request: Request) -> str:
    """取真实客户端 IP。

    网关（Caddy）把客户端 IP **追加**到 X-Forwarded-For 末尾，所以取最后一段；
    取第一段会被调用方自带的值污染。服务不直接对公网暴露端口，这是可接受的前提。
    """
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        parts = [part.strip() for part in forwarded.split(",") if part.strip()]
        if parts:
            return parts[-1]
    if request.headers.get("x-real-ip"):
        return str(request.headers["x-real-ip"]).strip()
    return request.client.host if request.client else "unknown"


def get_whitelist_rcon(settings: SettingsDep) -> WhitelistRcon:
    """每次请求构造一个轻量对象；连接本身是「一条命令一条连接」，无需池化。"""
    return WhitelistRcon(
        RconClient(
            settings.rcon_host,
            settings.rcon_port,
            settings.rcon_password,
            timeout=settings.rcon_timeout,
        )
    )


WhitelistDep = Annotated[WhitelistRcon, Depends(get_whitelist_rcon)]


def _audit_denied(
    db: DbSession,
    request: Request,
    *,
    user_id: str | None,
    detail: dict[str, Any],
) -> None:
    """被拒绝的请求也留痕（§10 第 10 条）。

    两层保护：按 IP 限流（防坏令牌刷爆审计表）；审计自身失败**绝不掩盖**原来的 401/403。
    """
    ip = client_ip(request)
    if not limiter.allow(f"mc-deny-audit:{ip}", DENY_AUDIT_ATTEMPTS, DENY_AUDIT_WINDOW_SECONDS):
        return
    try:
        deny_log(
            db,
            user_id=user_id,
            action=ACTION_DENY,
            target=None,
            result=str(detail.get("error") or "denied"),
            detail=detail,
            request_ip=ip,
        )
    except Exception:  # noqa: BLE001 - 审计失败不能把 401/403 变成 500
        logger.exception("写拒绝审计失败")


def current_identity(
    request: Request,
    settings: SettingsDep,
    db: DbDep,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)] = None,
) -> Identity:
    """验签 + 强制 `REQUIRED_SCOPE`。所有 `/v1/*` 都挂这个依赖。"""
    claims: dict[str, Any] | None = None
    try:
        if credentials is None or not credentials.credentials:
            raise api_error(401, "invalid_token", "缺少 Bearer 令牌", headers={"WWW-Authenticate": "Bearer"})
        if credentials.scheme.lower() != "bearer":
            raise api_error(401, "invalid_token", "认证方案必须是 Bearer", headers={"WWW-Authenticate": "Bearer"})

        claims = decode_access_token(settings, get_jwks_cache(settings), credentials.credentials)
        require_scope(claims, settings.required_scope)
    except StarletteHTTPException as exc:
        detail = exc.detail if isinstance(exc.detail, dict) else {"error": "denied", "error_description": str(exc.detail)}
        # 验签失败时拿不到可信的 sub，审计里就留空（表结构允许）
        _audit_denied(db, request, user_id=None if claims is None else str(claims.get("sub") or "") or None, detail={"status": exc.status_code, **detail})
        raise

    sub = str(claims.get("sub"))
    request.state.user_id = sub
    username = claims.get("preferred_username") or claims.get("username")
    return Identity(sub=sub, username=str(username) if username else None, claims=claims)


CurrentIdentity = Annotated[Identity, Depends(current_identity)]


def require_admin(
    request: Request,
    settings: SettingsDep,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)] = None,
) -> None:
    """管理接口鉴权（decision T2）：``ADMIN_TOKEN`` 未配置时**整组 403**（fail closed）。"""
    if not settings.admin_token:
        raise api_error(403, "forbidden", "管理接口未启用：请先配置 ADMIN_TOKEN")

    ip = client_ip(request)
    if not limiter.allow(f"mc-admin-auth:{ip}", ADMIN_AUTH_ATTEMPTS, ADMIN_AUTH_WINDOW_SECONDS):
        raise api_error(
            429,
            "rate_limited",
            "管理员认证尝试过于频繁，请稍后再试",
            headers={"Retry-After": str(ADMIN_AUTH_WINDOW_SECONDS)},
        )

    provided = credentials.credentials if credentials is not None else None
    # 两侧都转 bytes：compare_digest 不支持非 ASCII 的 str
    authorized = bool(provided) and secrets.compare_digest(
        str(provided).encode("utf-8"),
        settings.admin_token.encode("utf-8"),
    )
    if not authorized:
        logger.warning("mc-whitelist admin auth failed ip=%s", ip)
        # 绝不记录令牌原文
        raise api_error(403, "forbidden", "管理员令牌无效")

    limiter.reset(f"mc-admin-auth:{ip}")


AdminAuth = Annotated[None, Depends(require_admin)]


def check_submit_rate_limit(*, settings: Settings, user_id: str, ip: str) -> None:
    """提交 / 撤回按 `sub` + IP 双维度限流（§10 第 6 条），防脚本刷白名单。"""
    limit = settings.submit_attempts_per_window
    window = settings.submit_window_seconds
    for key in (f"mc-submit:{user_id}", f"mc-submit-ip:{ip}"):
        if not limiter.allow(key, limit, window):
            raise api_error(
                429,
                "rate_limited",
                "操作过于频繁，请稍后再试",
                headers={"Retry-After": str(window)},
            )
