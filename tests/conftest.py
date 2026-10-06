"""测试环境准备。

关键点（与 `sso/tests/conftest.py` 同一手法）：环境变量必须在**任何** app 模块被导入之前
写入（`config` 在实例化 Settings 时读环境），所以这里在 conftest 顶层就：

1. 起一个**假 JWKS 服务**（真实 HTTP + 真实 RS256 验签路径）；
2. 起一个**假 RCON 服务器**（复刻 §5.2 的四条实测行为）；
3. 把两者的地址写进环境变量；
4. 之后各用例用 `app.main.create_app()` 构造测试应用。

**全程不依赖真实 MC，也不装任何额外镜像**（§9.1）。

⚠️ 共享常量放在 `tests/helpers.py`，**不要**在用例里 `from tests.conftest import ...`
（那会导入第二份 conftest，产生两把不同的测试密钥）。
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tests.fake_jwks import FakeJwksServer  # noqa: E402
from tests.fake_rcon import FakeRconServer  # noqa: E402
from tests.helpers import (  # noqa: E402
    TEST_ADMIN_TOKEN,
    TEST_AUDIENCE,
    TEST_ISSUER,
    TEST_KID,
    TEST_KEY,
    TEST_OTHER_KEY,
    TEST_RCON_PASSWORD,
    TEST_REQUIRED_SCOPE,
    TEST_SCOPE,
    bearer,
    make_token,
)

_TEST_DIR = Path(tempfile.mkdtemp(prefix="mc-tests-"))

_JWKS = FakeJwksServer(TEST_KEY, TEST_KID, max_age=300).start()
_RCON = FakeRconServer(TEST_RCON_PASSWORD).start()

os.environ.update(
    {
        "SSO_ISSUER": TEST_ISSUER,
        "SSO_JWKS_URL": _JWKS.url,
        "JWT_AUDIENCE": TEST_AUDIENCE,
        "REQUIRED_SCOPE": TEST_REQUIRED_SCOPE,
        "DATABASE_URL": f"sqlite:///{(_TEST_DIR / 'mc-test.db').as_posix()}",
        "RCON_HOST": "127.0.0.1",
        "RCON_PORT": str(_RCON.port),
        "RCON_PASSWORD": TEST_RCON_PASSWORD,
        "RCON_TIMEOUT": "3",
        "MAX_NAMES_PER_USER": "2",
        "CORS_ORIGINS": "http://localhost:5173",
        "ADMIN_TOKEN": TEST_ADMIN_TOKEN,
        # 限流单独测（用 client_factory 调低阈值），别干扰其它用例
        "SUBMIT_ATTEMPTS_PER_WINDOW": "10000",
        "SUBMIT_WINDOW_SECONDS": "600",
        "LOG_LEVEL": "warning",
    }
)


# --------------------------------------------------------------- 基础设施


@pytest.fixture(scope="session", autouse=True)
def _servers():
    yield
    _RCON.stop()
    _JWKS.stop()


@pytest.fixture(scope="session")
def rcon() -> FakeRconServer:
    return _RCON


@pytest.fixture(scope="session")
def jwks() -> FakeJwksServer:
    return _JWKS


@pytest.fixture(scope="session")
def signing_key():
    return TEST_KEY


@pytest.fixture(scope="session")
def other_signing_key():
    return TEST_OTHER_KEY


@pytest.fixture(scope="session")
def constants() -> dict[str, str]:
    return {
        "issuer": TEST_ISSUER,
        "audience": TEST_AUDIENCE,
        "kid": TEST_KID,
        "scope": TEST_REQUIRED_SCOPE,
        "admin_token": TEST_ADMIN_TOKEN,
        "rcon_password": TEST_RCON_PASSWORD,
    }


# ------------------------------------------------------------------- 应用


@pytest.fixture(scope="session")
def client():
    from fastapi.testclient import TestClient

    from app.main import create_app

    with TestClient(create_app()) as test_client:
        yield test_client


@pytest.fixture
def client_factory():
    """构造「改了某几项配置」的应用实例（依赖覆盖，避免动全局 lru_cache）。"""
    from fastapi.testclient import TestClient

    from app.config import Settings, get_settings
    from app.main import create_app

    created = []

    def _make(**overrides) -> TestClient:
        settings = Settings(**overrides)
        app = create_app(settings)
        app.dependency_overrides[get_settings] = lambda: settings
        test_client = TestClient(app)
        test_client.__enter__()
        created.append(test_client)
        return test_client

    yield _make
    for test_client in created:
        test_client.__exit__(None, None, None)


# ------------------------------------------------------------------ 令牌


@pytest.fixture
def token():
    def _make(**kwargs) -> str:
        return make_token(**kwargs)

    return _make


@pytest.fixture
def auth():
    def _make(sub: str = "user_test", scope: str | None = None, **kwargs) -> dict[str, str]:
        return bearer(make_token(sub=sub, scope=scope if scope is not None else TEST_SCOPE, **kwargs))

    return _make


# --------------------------------------------------------------- 隔离与工具


@pytest.fixture(autouse=True)
def _clean_state():
    """限流与数据库都是进程内状态，用例之间必须隔离（假 RCON 也一起复位）。"""
    from sqlalchemy import delete

    from app.config import get_settings
    from app.db import init_db, session_scope
    from app.errors import limiter
    from app.models import AuditLog, McName, WhitelistCache

    init_db(get_settings())
    with session_scope() as session:
        session.execute(delete(McName))
        session.execute(delete(AuditLog))
        session.execute(delete(WhitelistCache))
        session.commit()

    limiter.clear()
    _RCON.reset()
    yield
    limiter.clear()


@pytest.fixture
def make_rcon():
    """另一个假 RCON 实例（用于故障注入：lying_add / 密码不对）。"""
    created: list[FakeRconServer] = []

    def _make(**kwargs) -> FakeRconServer:
        server = FakeRconServer(kwargs.pop("password", TEST_RCON_PASSWORD), **kwargs).start()
        created.append(server)
        return server

    yield _make
    for server in created:
        server.stop()


@pytest.fixture
def db_rows():
    """直接读库断言（审计、绑定），并能拿一个「没人监听的端口」。"""
    from sqlalchemy import select

    from app.db import session_scope
    from app.models import AuditLog, McName

    def _rows(model, **filters):
        with session_scope() as session:
            statement = select(model)
            for column, value in filters.items():
                statement = statement.where(getattr(model, column) == value)
            return list(session.scalars(statement).all())

    from tests.fake_rcon import free_port

    return {
        "names": lambda **filters: _rows(McName, **filters),
        "audits": lambda **filters: _rows(AuditLog, **filters),
        "free_port": free_port,
    }
