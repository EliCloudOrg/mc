"""管理面与对账（mc.md §6.2、§7、决策 T2）。

管理端点用 `ADMIN_TOKEN` 保护，**未配置时整组 403**（fail closed）；它与用户侧的
SSO access token 是两套完全独立的凭据，不能互相替代。
"""

from __future__ import annotations

import pytest

from tests.helpers import TEST_ADMIN_TOKEN


def admin(value: str = TEST_ADMIN_TOKEN) -> dict[str, str]:
    return {"Authorization": f"Bearer {value}"}


def submit(client, headers, name: str):
    return client.post("/v1/names", json={"name": name, "note": "管理面测试"}, headers=headers)


# ------------------------------------------------------------------ 鉴权


def test_admin_group_is_403_when_token_not_configured(client_factory) -> None:
    scoped = client_factory(admin_token=None)
    for path in ("/v1/admin/names", "/v1/admin/audit"):
        response = scoped.get(path, headers=admin())
        assert response.status_code == 403
        assert response.json()["error"] == "forbidden"


def test_admin_group_is_403_with_wrong_token(client) -> None:
    assert client.get("/v1/admin/names", headers=admin("nope")).status_code == 403


def test_admin_group_is_403_without_any_token(client) -> None:
    assert client.get("/v1/admin/names").status_code == 403


def test_admin_token_cannot_be_used_as_user_token(client) -> None:
    """ADMIN_TOKEN 不是 JWT，不能拿去调用户端点。"""
    response = client.get("/v1/names", headers=admin())
    assert response.status_code == 401


def test_user_token_cannot_be_used_as_admin_token(client, auth) -> None:
    """SSO access token 也不是管理令牌（两套凭据互不通用）。"""
    headers = auth(sub="user_not_admin")
    assert client.get("/v1/admin/names", headers=headers).status_code == 403


# ------------------------------------------------------------------ 列表与审计


def test_admin_lists_all_bindings_with_notes(client, auth) -> None:
    assert submit(client, auth(sub="user_admin_list"), "AdminSeeMe").status_code == 201

    body = client.get("/v1/admin/names", headers=admin()).json()

    assert body["total"] == 1
    entry = body["names"][0]
    assert entry["sub"] == "user_admin_list"
    assert entry["name"] == "adminseeme"
    assert entry["note"] == "管理面测试"  # T8：管理员可见备注全文


def test_admin_audit_can_filter_by_sub_and_action(client, auth) -> None:
    assert submit(client, auth(sub="user_audit_admin"), "AuditViaAdmin").status_code == 201
    assert submit(client, auth(sub="user_audit_admin"), "AuditViaAdmin").status_code == 200

    by_sub = client.get("/v1/admin/audit?user_id=user_audit_admin", headers=admin()).json()
    assert by_sub["total"] == 2
    by_action = client.get("/v1/admin/audit?action=add&user_id=user_audit_admin", headers=admin()).json()
    assert by_action["total"] == 2
    assert {log["action"] for log in by_action["logs"]} == {"add"}


# ------------------------------------------------------------------ 强制移除


def test_admin_force_remove(client, auth, rcon, db_rows) -> None:
    entry = submit(client, auth(sub="user_forced"), "ForceMe").json()
    assert "forceme" in rcon.players

    body = client.delete(f"/v1/admin/names/{entry['id']}", headers=admin()).json()

    assert body["status"] == "removed"
    assert body["removed_by"] == "admin"
    assert rcon.players == []
    row = db_rows["names"](id=entry["id"])[0]
    assert row.status == "removed" and row.removed_by == "admin"


def test_admin_force_remove_unknown_is_404(client) -> None:
    assert client.delete("/v1/admin/names/mn_9999", headers=admin()).status_code == 404


# ------------------------------------------------------------------ 对账


def test_reconcile_reports_drift_then_applies(client, auth, rcon) -> None:
    assert submit(client, auth(sub="user_reconcile"), "ReconcileMe").status_code == 201
    rcon.players.clear()  # 人为制造漂移：库里 active、白名单里没有

    report = client.post("/v1/admin/reconcile", json={"apply": False}, headers=admin()).json()
    assert report["missing"] == ["reconcileme"]
    assert report["applied"] == []
    assert rcon.players == [], "apply=false 不允许改白名单"

    applied = client.post("/v1/admin/reconcile", json={"apply": True}, headers=admin()).json()
    assert applied["applied"] == ["reconcileme"]
    assert rcon.players == ["reconcileme"]


def test_reconcile_reports_extra_but_never_deletes(client, rcon) -> None:
    rcon.players.append("manualonly")  # 管理员手工加的白名单，库里没有

    report = client.post("/v1/admin/reconcile", json={"apply": True}, headers=admin()).json()

    assert report["extra"] == ["manualonly"]
    assert "manualonly" in rcon.players, "绝不能自动删除别人手工加的白名单"
    assert report["applied"] == []


def test_reconcile_rejects_unknown_fields(client) -> None:
    response = client.post("/v1/admin/reconcile", json={"apply": True, "force": True}, headers=admin())
    assert response.status_code == 400


@pytest.mark.parametrize("payload", [{}, {"apply": False}])
def test_reconcile_defaults_to_report_only(client, auth, rcon, payload) -> None:
    assert submit(client, auth(sub="user_reconcile2"), "ReconcileTwo").status_code == 201
    rcon.players.clear()
    report = client.post("/v1/admin/reconcile", json=payload, headers=admin()).json()
    assert report["apply"] is False
    assert report["applied"] == []
    assert rcon.players == []
