"""FastAPI 应用装配。

服务内路由（对外由网关剥掉 `/mc` 前缀，见 mc.md §6.3）：

    POST   /v1/names              DELETE /v1/names/{id}      GET /v1/me
    GET    /v1/names              GET    /healthz
    GET    /v1/admin/names        GET    /v1/admin/audit
    DELETE /v1/admin/names/{id}   POST   /v1/admin/reconcile

`/docs`、`/redoc`、`/openapi.json` 全部关掉（§10 第 8 条：不把内部接口暴露到公网）。
"""

from __future__ import annotations

import logging
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import __version__
from .config import Settings, get_settings
from .db import init_db, session_scope
from .deps import client_ip
from .errors import limiter
from .models import ACTION_DENY
from .routers import names, ops
from .whitelist import audit

logger = logging.getLogger("mc")

# 400 也留痕（§10 第 10 条），但按 IP 限流：有效令牌的客户端刷畸形请求不该刷爆审计表
INVALID_REQUEST_AUDIT_ATTEMPTS = 60
INVALID_REQUEST_AUDIT_WINDOW_SECONDS = 300


def configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, str(level).strip().upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings: Settings = app.state.settings
    logger.info("mc starting version=%s %s", __version__, settings.log_summary())
    if not settings.scope_enforced:
        logger.warning("REQUIRED_SCOPE 为空：scope 校验已关闭（只应在本地调试时如此）")
    if not settings.admin_token:
        logger.warning("ADMIN_TOKEN 未配置：/v1/admin/* 整组 403（fail closed）")
    yield
    logger.info("mc stopped")


def _audit_invalid_request(request: Request, details: list[dict[str, object]]) -> None:
    """请求体校验失败也留痕（§10 第 10 条）。

    best-effort：审计自身失败**绝不改变**那个 400 响应；写入按 IP 限流。
    """
    ip = client_ip(request)
    if not limiter.allow(
        f"mc-audit-400:{ip}", INVALID_REQUEST_AUDIT_ATTEMPTS, INVALID_REQUEST_AUDIT_WINDOW_SECONDS
    ):
        return
    try:
        with session_scope() as session:
            audit(
                session,
                user_id=getattr(request.state, "user_id", None),
                action=ACTION_DENY,
                target=None,
                result="invalid_request",
                detail={
                    "path": request.url.path,
                    "errors": [str(item.get("msg")) for item in details][:5],
                },
                request_ip=ip,
            )
            session.commit()
    except Exception:  # noqa: BLE001 - 审计失败不能把 400 变成 500
        logger.exception("写 400 审计失败")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level)

    init_db(settings)

    app = FastAPI(
        title="EliCloud MC Whitelist",
        version=__version__,
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.settings = settings

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.middleware("http")
    async def access_log(request: Request, call_next):
        started = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            # 令牌原文绝不进日志；这里只记路径与耗时
            logger.exception("unhandled error path=%s", request.url.path)
            raise
        elapsed_ms = (time.perf_counter() - started) * 1000
        user_id = getattr(request.state, "user_id", "-")
        logger.info(
            "%s %s -> %s sub=%s %.1fms",
            request.method,
            request.url.path,
            response.status_code,
            user_id,
            elapsed_ms,
        )
        return response

    @app.exception_handler(StarletteHTTPException)
    async def http_exception_handler(_request: Request, exc: StarletteHTTPException) -> JSONResponse:
        detail = exc.detail
        if isinstance(detail, dict) and "error" in detail:
            content = detail
        else:
            content = {"error": "http_error", "error_description": str(detail)}
        return JSONResponse(status_code=exc.status_code, content=content, headers=dict(exc.headers or {}))

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
        details = [
            {"loc": list(item.get("loc", ())), "msg": item.get("msg"), "type": item.get("type")}
            for item in exc.errors()
        ]
        _audit_invalid_request(request, details)
        return JSONResponse(
            status_code=400,
            content={"error": "invalid_request", "error_description": "请求参数不合法", "details": details},
        )

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(_request: Request, exc: Exception) -> JSONResponse:
        logger.exception("unhandled exception")
        return JSONResponse(
            status_code=500,
            content={"error": "server_error", "error_description": "服务内部错误"},
        )

    app.include_router(names.router)
    app.include_router(ops.router)
    app.include_router(ops.admin)

    return app


app = create_app()
