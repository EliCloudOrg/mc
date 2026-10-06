"""鉴权与 scope（mc-whitelist.md §6.1、§9.1 的前几条）。"""

from __future__ import annotations

import time

import jwt
import pytest

from tests.helpers import TEST_AUDIENCE, TEST_ISSUER, TEST_KID, TEST_OTHER_KEY, TEST_REQUIRED_SCOPE


def test_missing_token_is_401(client) -> None:
    response = client.get("/v1/names")
    assert response.status_code == 401
    assert response.json()["error"] == "invalid_token"


def test_non_bearer_scheme_is_401(client, token) -> None:
    response = client.get("/v1/names", headers={"Authorization": f"Basic {token()}"})
    assert response.status_code == 401


def test_garbage_token_is_401(client) -> None:
    response = client.get("/v1/names", headers={"Authorization": "Bearer not-a-jwt-at-all"})
    assert response.status_code == 401


def test_expired_token_is_401(client, auth) -> None:
    # 超过 30s 时钟偏移容忍窗
    assert client.get("/v1/names", headers=auth(expires_in=-120)).status_code == 401


def test_wrong_signature_is_401(client, token) -> None:
    value = token(key=TEST_OTHER_KEY)
    response = client.get("/v1/names", headers={"Authorization": f"Bearer {value}"})
    assert response.status_code == 401
    assert "校验失败" in response.json()["error_description"]


def test_wrong_issuer_is_401(client, token) -> None:
    value = token(issuer="https://evil.example/auth")
    assert client.get("/v1/names", headers={"Authorization": f"Bearer {value}"}).status_code == 401


def test_id_token_masquerading_as_access_token_is_401(client, token) -> None:
    """`id_token` 的 aud 是 client_id，必须在这里被拒（architecture.md §16.1）。"""
    value = token(audience="elipese-web")
    response = client.get("/v1/names", headers={"Authorization": f"Bearer {value}"})
    assert response.status_code == 401
    assert "aud" in response.json()["error_description"]


def test_unknown_kid_is_401(client, token) -> None:
    value = token(kid="no-such-kid")
    assert client.get("/v1/names", headers={"Authorization": f"Bearer {value}"}).status_code == 401


def test_hs256_is_rejected(client) -> None:
    """对称算法必须被拒：业务服务只应持有公钥。"""
    now = int(time.time())
    value = jwt.encode(
        {"iss": TEST_ISSUER, "aud": TEST_AUDIENCE, "sub": "u", "iat": now, "exp": now + 600, "scope": TEST_REQUIRED_SCOPE},
        "a-shared-secret-that-is-long-enough-to-avoid-warnings",
        algorithm="HS256",
        headers={"kid": TEST_KID},
    )
    response = client.get("/v1/names", headers={"Authorization": f"Bearer {value}"})
    assert response.status_code == 401
    assert "算法" in response.json()["error_description"]


def test_alg_none_is_rejected(client) -> None:
    now = int(time.time())
    value = jwt.encode(
        {"iss": TEST_ISSUER, "aud": TEST_AUDIENCE, "sub": "u", "iat": now, "exp": now + 600, "scope": TEST_REQUIRED_SCOPE},
        None,
        algorithm="none",
        headers={"kid": TEST_KID},
    )
    assert client.get("/v1/names", headers={"Authorization": f"Bearer {value}"}).status_code == 401


# ------------------------------------------------------------------- scope


def test_scope_without_mc_whitelist_is_403(client, auth, db_rows) -> None:
    headers = auth(sub="user_no_scope", scope="openid profile email pdf:read")
    response = client.get("/v1/names", headers=headers)
    assert response.status_code == 403
    assert response.json()["error"] == "insufficient_scope"
    # 失败也留痕（§10 第 10 条），且能定位到 sub
    denials = db_rows["audits"](action="deny")
    assert len(denials) == 1 and denials[0].user_id == "user_no_scope"


def test_empty_scope_is_403(client, auth) -> None:
    assert client.get("/v1/names", headers=auth(scope="")).status_code == 403


def test_scope_is_matched_as_whole_word(client, auth) -> None:
    """`mc:whitelistX` 不能当命中（整词匹配）。"""
    headers = auth(scope="openid mc:whitelistX")
    assert client.get("/v1/names", headers=headers).status_code == 403


def test_disabling_required_scope_allows_call(client_factory, token) -> None:
    """`REQUIRED_SCOPE=""` 是显式调试开关：同一令牌可以通过（锁住这个行为）。"""
    scoped_client = client_factory(required_scope="")
    value = token(scope="openid profile")
    response = scoped_client.get("/v1/names", headers={"Authorization": f"Bearer {value}"})
    assert response.status_code == 200


def test_token_value_never_leaks_into_response_or_logs(client, auth, caplog) -> None:
    import logging

    headers = auth(sub="user_leak")
    raw_token = headers["Authorization"].split()[1]

    with caplog.at_level(logging.INFO):
        response = client.get("/v1/me", headers=headers)

    assert response.status_code == 200
    assert raw_token not in response.text
    assert all(raw_token not in record.getMessage() for record in caplog.records)


@pytest.mark.parametrize("path", ["/v1/names", "/v1/me"])
def test_every_v1_endpoint_requires_a_token(client, path) -> None:
    assert client.get(path).status_code == 401
