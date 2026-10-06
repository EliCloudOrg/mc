"""全部配置从环境变量读取（风格与 `sso/app/config.py` 一致）。

三处归属（deploy-framework.md §2.4）：

* 运行时变量 → 服务器 `/srv/mc/app.env`（600），由 `deploy.sh` 读出后注入容器；
* 可版本化的默认值 → 本文件与 `docker-compose.yml`；
* **`RCON_PASSWORD` 只从环境变量来，没有默认值** —— 缺了就直接启动失败（fail closed），
  绝不允许"忘了配密码却照常跑起来"。
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore", case_sensitive=False)

    # ---- SSO 接入（architecture.md §8）--------------------------------------
    # 必须与 SSO 的 PUBLIC_BASE_URL 逐字一致，否则所有令牌都验签失败。
    sso_issuer: str = "https://146.56.237.33/auth"
    sso_jwks_url: str = "https://146.56.237.33/auth/.well-known/jwks.json"
    jwt_audience: str = "elicloud-services"
    # T1（mc.md §12.2）：令牌必须含该 scope；空串 = 关闭校验（仅限本地调试）
    required_scope: str = "mc:whitelist"
    jwks_cache_seconds: int = 300
    jwt_leeway_seconds: int = 30  # exp/nbf 的时钟偏移容忍
    jwks_timeout_seconds: float = 5.0

    # ---- 存储 ---------------------------------------------------------------
    database_url: str = "sqlite:////data/mc.db"

    # ---- RCON（mc.md §5）-----------------------------------------
    rcon_host: str = "urania-mc"
    rcon_port: int = 25575
    # 没有默认值：缺 RCON_PASSWORD 即启动失败
    rcon_password: str
    rcon_timeout: float = 5.0

    # ---- 业务 ---------------------------------------------------------------
    max_names_per_user: int = 2
    cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173"
    # 管理接口令牌；未配置时整组 403（fail closed）
    admin_token: str | None = None
    # 提交 / 撤回的限流（按 sub + IP）
    submit_attempts_per_window: int = 10
    submit_window_seconds: int = 600
    log_level: str = "info"

    # ------------------------------------------------------------------ 校验
    @field_validator("sso_issuer")
    @classmethod
    def _check_issuer(cls, value: str) -> str:
        cleaned = value.strip().rstrip("/")
        if not cleaned.startswith(("http://", "https://")):
            raise ValueError("SSO_ISSUER 必须以 http:// 或 https:// 开头")
        if cleaned.endswith("/v1") or "/.well-known" in cleaned:
            raise ValueError("SSO_ISSUER 只应到服务前缀（如 .../auth），不要带 /v1 或 /.well-known")
        return cleaned

    @field_validator("sso_jwks_url")
    @classmethod
    def _check_jwks_url(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned.startswith(("http://", "https://")):
            raise ValueError("SSO_JWKS_URL 必须以 http:// 或 https:// 开头")
        return cleaned

    @field_validator("required_scope")
    @classmethod
    def _check_required_scope(cls, value: str) -> str:
        cleaned = value.strip()
        if cleaned and any(ch.isspace() for ch in cleaned):
            raise ValueError("REQUIRED_SCOPE 只能是单个 scope（不能含空格）")
        return cleaned

    @field_validator("rcon_host")
    @classmethod
    def _check_rcon_host(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("RCON_HOST 不能为空")
        return cleaned

    @field_validator("rcon_password")
    @classmethod
    def _check_rcon_password(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError("RCON_PASSWORD 不能为空（放在服务器 /srv/mc/app.env）")
        return value

    @field_validator("rcon_port")
    @classmethod
    def _check_rcon_port(cls, value: int) -> int:
        if not (1 <= int(value) <= 65535):
            raise ValueError("RCON_PORT 必须在 1..65535")
        return int(value)

    @field_validator("rcon_timeout", "jwks_timeout_seconds")
    @classmethod
    def _check_positive_timeout(cls, value: float) -> float:
        if float(value) <= 0:
            raise ValueError("超时必须是正数")
        return float(value)

    @field_validator("max_names_per_user")
    @classmethod
    def _check_quota(cls, value: int) -> int:
        if int(value) < 1:
            raise ValueError("MAX_NAMES_PER_USER 至少为 1")
        return int(value)

    # ------------------------------------------------------------ 派生属性
    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]

    @property
    def jwks_uri(self) -> str:
        return self.sso_jwks_url

    @property
    def scope_enforced(self) -> bool:
        """REQUIRED_SCOPE 为空 = 显式关闭校验（只允许本地调试时这么做）。"""
        return bool(self.required_scope)

    def log_summary(self) -> dict[str, object]:
        """启动日志用的摘要 —— **绝不含 RCON 密码**。"""
        return {
            "issuer": self.sso_issuer,
            "jwks_url": self.sso_jwks_url,
            "audience": self.jwt_audience,
            "required_scope": self.required_scope or "(disabled)",
            "database_url": self.database_url,
            "rcon": f"{self.rcon_host}:{self.rcon_port}",
            "rcon_password_set": bool(self.rcon_password),
            "max_names_per_user": self.max_names_per_user,
            "admin_token_set": bool(self.admin_token),
            "cors_origins": self.cors_origin_list,
        }


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


def reset_settings_cache() -> None:
    """测试用：清掉缓存，让下一次 get_settings() 重新读环境变量。"""
    get_settings.cache_clear()
