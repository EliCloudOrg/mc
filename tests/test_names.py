"""契约与业务规则测试（mc.md §6.2 处理顺序、§7 并发一致性、§9.1 清单）。"""

from __future__ import annotations

import pytest

from tests.helpers import TEST_ISSUER, TEST_REQUIRED_SCOPE


def submit(client, headers, name: str, note: str = "帮朋友申请，主号被封禁"):
    return client.post("/v1/names", json={"name": name, "note": note}, headers=headers)


# ---------------------------------------------------------------- 正常路径


def test_first_submit_creates_entry_and_hits_rcon_three_times(client, auth, rcon, db_rows) -> None:
    headers = auth(sub="user_add")

    response = submit(client, headers, "ProbeTester")

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["name"] == "probetester"  # 归一为小写
    assert body["name_display"] == "ProbeTester"  # 保留原始写法
    assert body["status"] == "active"
    assert body["uuid"] is None  # 绝不构造 UUID（§5.2 结论 4）
    assert body["note"] == "帮朋友申请，主号被封禁"

    # 恰好 list → add → list 三条命令（§9.1）
    assert rcon.commands == ["whitelist list", "whitelist add probetester", "whitelist list"]
    assert rcon.players == ["probetester"]

    rows = db_rows["names"](status="active")
    assert len(rows) == 1 and rows[0].user_id == "user_add"


def test_duplicate_submit_is_idempotent_and_skips_rcon(client, auth, rcon) -> None:
    headers = auth(sub="user_dup")
    first = submit(client, headers, "DupTester")
    assert first.status_code == 201

    rcon.commands.clear()
    second = submit(client, headers, "duptester")  # 大小写不同 = 同一个名字

    assert second.status_code == 200, "重复提交同名必须幂等返回 200"
    assert second.json()["id"] == first.json()["id"]
    assert rcon.add_commands == [], "幂等分支不允许再调一次 RCON add"


def test_list_names_and_quota_then_include_removed(client, auth) -> None:
    headers = auth(sub="user_list")
    submit(client, headers, "ListOne")

    body = client.get("/v1/names", headers=headers).json()
    assert body["sub"] == "user_list"
    assert body["quota"] == {"limit": 2, "used": 1, "remaining": 1}
    assert len(body["names"]) == 1

    entry_id = body["names"][0]["id"]
    assert client.delete(f"/v1/names/{entry_id}", headers=headers).status_code == 200

    assert client.get("/v1/names", headers=headers).json()["names"] == []
    with_removed = client.get("/v1/names?include_removed=true", headers=headers).json()
    assert len(with_removed["names"]) == 1
    assert with_removed["names"][0]["status"] == "removed"


def test_me_returns_claims_only(client, auth) -> None:
    body = client.get("/v1/me", headers=auth(sub="user_me")).json()
    assert body["sub"] == "user_me"
    assert body["issuer"] == TEST_ISSUER
    assert TEST_REQUIRED_SCOPE in body["scope"]


# ------------------------------------------------------------------ 名额


def test_quota_exceeded_then_delete_frees_a_slot(client, auth, rcon) -> None:
    headers = auth(sub="user_quota")
    assert submit(client, headers, "QuotaOne").status_code == 201
    assert submit(client, headers, "QuotaTwo").status_code == 201

    third = submit(client, headers, "QuotaThree")
    assert third.status_code == 409
    assert third.json()["error"] == "quota_exceeded"
    assert "quotathree" not in rcon.players, "配额不足时绝不能已经写进白名单"

    entry_id = client.get("/v1/names", headers=headers).json()["names"][0]["id"]
    assert client.delete(f"/v1/names/{entry_id}", headers=headers).status_code == 200

    assert submit(client, headers, "QuotaThree").status_code == 201


# ------------------------------------------------------------------ 唯一性


def test_name_taken_by_another_sub(client, auth) -> None:
    assert submit(client, auth(sub="user_owner"), "TakenName").status_code == 201

    response = submit(client, auth(sub="user_other"), "takenname")

    assert response.status_code == 409
    assert response.json()["error"] == "name_taken"


def test_name_already_in_whitelist_but_not_in_db_conflicts(client, auth, rcon) -> None:
    """管理员手工加进白名单、库里没有 → 视为已被占用，返回 409（§6.2 第 4 步）。"""
    rcon.players.append("manualguy")

    response = submit(client, auth(sub="user_manual"), "ManualGuy")

    assert response.status_code == 409
    assert response.json()["error"] == "name_taken"


def test_delete_other_users_entry_is_404_and_whitelist_untouched(client, auth, rcon) -> None:
    owner = auth(sub="user_owner2")
    entry = submit(client, owner, "OwnerOnly").json()

    response = client.delete(f"/v1/names/{entry['id']}", headers=auth(sub="user_thief"))

    assert response.status_code == 404  # 不泄露他人资源是否存在
    assert "owneronly" in rcon.players


def test_delete_unknown_entry_is_404(client, auth) -> None:
    assert client.delete("/v1/names/mn_9999", headers=auth(sub="user_none")).status_code == 404


def test_delete_then_reapply_immediately(client, auth, rcon) -> None:
    """T3：本期不加撤回冷却期，删掉可以立刻重新申请同名。"""
    headers = auth(sub="user_del")
    entry = submit(client, headers, "DeleteMe").json()
    assert "deleteme" in rcon.players

    response = client.delete(f"/v1/names/{entry['id']}", headers=headers)
    assert response.status_code == 200
    assert response.json()["status"] == "removed"
    assert rcon.players == []

    assert submit(client, headers, "DeleteMe").status_code == 201


def test_delete_is_idempotent(client, auth, rcon) -> None:
    headers = auth(sub="user_del2")
    entry = submit(client, headers, "DeleteTwice").json()

    assert client.delete(f"/v1/names/{entry['id']}", headers=headers).status_code == 200
    rcon.commands.clear()
    again = client.delete(f"/v1/names/{entry['id']}", headers=headers)

    assert again.status_code == 200
    assert again.json()["status"] == "removed"
    assert rcon.commands == [], "已撤回的条目不应该再调 RCON"


# ------------------------------------------------------------------ 输入约束


@pytest.mark.parametrize(
    "bad_name",
    ["ab", "中文名字", "a b", "a;rm -rf", "A" * 17, "", "user-name", "user.name", "a" * 16 + "b"],
)
def test_invalid_name_is_400(client, auth, bad_name: str) -> None:
    response = submit(client, auth(sub="user_bad_name"), bad_name)
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_request"


@pytest.mark.parametrize("bad_note", ["", "x" * 201, "bad\x00note", "line\nbreak"])
def test_invalid_note_is_400(client, auth, bad_note: str) -> None:
    response = submit(client, auth(sub="user_bad_note"), "NoteTester", note=bad_note)
    assert response.status_code == 400


def test_missing_note_is_400(client, auth) -> None:
    response = client.post("/v1/names", json={"name": "NoNote"}, headers=auth(sub="user_no_note"))
    assert response.status_code == 400


def test_unknown_field_is_400(client, auth) -> None:
    response = client.post(
        "/v1/names",
        json={"name": "ExtraField", "note": "x", "is_admin": True},
        headers=auth(sub="user_extra"),
    )
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_request"


def test_valid_name_boundary_lengths_are_accepted(client, auth) -> None:
    headers = auth(sub="user_len")
    assert submit(client, headers, "abc").status_code == 201  # 3 字符下界
    assert submit(client, headers, "x" * 16).status_code == 201  # 16 字符上界


def test_invalid_request_is_audited(client, auth, db_rows) -> None:
    """400 也要留痕（§10 第 10 条：每一次尝试都写 audit_logs）。"""
    assert submit(client, auth(sub="user_400"), "ab").status_code == 400

    rows = db_rows["audits"](result="invalid_request")
    assert len(rows) == 1
    assert rows[0].action == "deny"
    assert rows[0].user_id == "user_400"  # 令牌有效但请求体不合法，身份是可确定的


# ------------------------------------------------------------------ RCON 故障


def test_rcon_unreachable_is_503_and_does_not_persist(client_factory, token, db_rows) -> None:
    free = db_rows["free_port"]()
    scoped_client = client_factory(rcon_port=free)
    headers = {"Authorization": f"Bearer {token(sub='user_down')}"}

    response = scoped_client.post("/v1/names", json={"name": "DownGuy", "note": "x"}, headers=headers)

    assert response.status_code == 503
    assert response.json()["error"] == "rcon_unavailable"
    assert db_rows["names"]() == [], "RCON 不可达时绝不能有「已生效」的记录"
    assert len(db_rows["audits"](result="rcon_error")) == 1


def test_add_reply_is_not_trusted_readback_decides(client_factory, token, make_rcon, db_rows) -> None:
    """铁律：不信 add 的回执。回执说成功但白名单没变 → 503 且不落库（§5.3）。"""
    liar = make_rcon(mode="lying_add")
    scoped_client = client_factory(rcon_port=liar.port)
    headers = {"Authorization": f"Bearer {token(sub='user_lie')}"}

    response = scoped_client.post("/v1/names", json={"name": "LyingGuy", "note": "x"}, headers=headers)

    assert response.status_code == 503
    assert liar.players == [], "假服务器的白名单里确实没有它"
    assert db_rows["names"]() == []


def test_withdraw_is_not_confirmed_leaves_db_untouched(client_factory, token, rcon, db_rows) -> None:
    """撤回时若回读不确认，库状态**不动**（宁可状态不变，也不要库与白名单不一致）。"""
    from tests.fake_rcon import free_port

    headers = {"Authorization": f"Bearer {token(sub='user_half')}"}
    shared_client = client_factory()  # 与共享假 RCON 同一目标
    entry = submit(shared_client, headers, "HalfGuy").json()

    # 换一个「连不上」的 RCON 目标来执行撤回
    broken = client_factory(rcon_port=free_port())
    response = broken.delete(f"/v1/names/{entry['id']}", headers=headers)

    assert response.status_code == 503
    row = db_rows["names"](id=entry["id"])[0]
    assert row.status == "active", "撤回未被确认时库状态必须保持 active"
    assert "halfguy" in rcon.players


# ------------------------------------------------------------------ 审计


def test_audit_records_success_idempotent_and_conflict(client, auth, db_rows) -> None:
    headers = auth(sub="user_audit")
    assert submit(client, headers, "AuditOne").status_code == 201
    assert submit(client, headers, "AuditOne").status_code == 200  # 幂等
    assert submit(client, auth(sub="user_audit2"), "AuditOne").status_code == 409  # 冲突

    results = {row.result for row in db_rows["audits"]()}
    assert {"ok", "idempotent", "conflict"} <= results

    ok_rows = db_rows["audits"](result="ok")
    assert len(ok_rows) == 1
    assert ok_rows[0].user_id == "user_audit"
    assert ok_rows[0].action == "add"
    assert ok_rows[0].target == "auditone"


# ------------------------------------------------------------------ 限流


def test_submit_is_rate_limited(client_factory, token) -> None:
    scoped_client = client_factory(submit_attempts_per_window=2, submit_window_seconds=600)
    headers = {"Authorization": f"Bearer {token(sub='user_rl')}"}

    assert submit(scoped_client, headers, "RateOne").status_code == 201
    assert submit(scoped_client, headers, "RateTwo").status_code == 201

    third = submit(scoped_client, headers, "RateThree")
    assert third.status_code == 429
    assert third.json()["error"] == "rate_limited"
    assert third.headers.get("Retry-After") == "600"


# ------------------------------------------------------------------ 健康检查


def test_healthz_reports_rcon_reachable(client, rcon) -> None:
    body = client.get("/healthz").json()
    assert body["status"] == "ok"
    assert body["rcon"]["reachable"] is True
    assert body["whitelist_count"] == 0
    assert body["sso_issuer"] == TEST_ISSUER


def test_healthz_is_503_when_rcon_is_down(client_factory, db_rows) -> None:
    scoped_client = client_factory(rcon_port=db_rows["free_port"]())
    response = scoped_client.get("/healthz")
    assert response.status_code == 503
    assert response.json()["rcon"]["reachable"] is False


def test_healthz_needs_no_token(client) -> None:
    assert client.get("/healthz").status_code == 200


def test_openapi_and_docs_are_disabled(client) -> None:
    """§10 第 8 条：不把内部接口（Swagger）暴露到公网。"""
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(path).status_code == 404
