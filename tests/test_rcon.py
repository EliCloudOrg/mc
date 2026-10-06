"""RCON 协议层测试（对应 mc-whitelist.md §5 的四条实测行为）。

这里刻意直接读原始包，证明「假服务器确实复刻了线上行为」，从而让上层用例的
「先空包后内容」不是假设而是被验证过的前提。
"""

from __future__ import annotations

import socket

import pytest

from app.rcon import (
    RconClient,
    RconError,
    WhitelistRcon,
    encode_packet,
    guard_name,
    parse_whitelist_list,
    read_packet,
)
from tests.helpers import TEST_RCON_PASSWORD as PASSWORD
from tests.fake_rcon import free_port


# ------------------------------------------------------------------ 响应解析


@pytest.mark.parametrize(
    ("response", "expected"),
    [
        ("There are no whitelisted players", []),
        ("There are 1 whitelisted player(s): probetester", ["probetester"]),
        ("There are 2 whitelisted player(s): alpha, beta", ["alpha", "beta"]),
        ("There are 2 whitelisted player(s): ALPHA, Beta", ["alpha", "beta"]),
        ("There are 3 whitelisted player(s): a, b, c\n", ["a", "b", "c"]),
    ],
)
def test_parse_whitelist_list(response: str, expected: list[str]) -> None:
    assert parse_whitelist_list(response) == expected


def test_parse_whitelist_list_rejects_unparseable() -> None:
    with pytest.raises(RconError):
        parse_whitelist_list("Something unexpected happened")


# ------------------------------------------------------------------ 注入防线


@pytest.mark.parametrize(
    "bad",
    ["ab", "a b", "a;rm -rf", "名字带中文", "x" * 17, "", "user-name", "user.name", "drop table"],
)
def test_guard_name_rejects(bad: str) -> None:
    with pytest.raises(RconError):
        guard_name(bad)


@pytest.mark.parametrize("good", ["abc", "ProbeTester", "A_1", "x" * 16])
def test_guard_name_accepts(good: str) -> None:
    assert guard_name(good) == good


# ------------------------------------------------------------------ 端到端行为


def test_fake_server_really_sends_empty_packet_first(make_rcon) -> None:
    """行为 2 的证据：同一条命令，服务端先回空包、再回真实内容。"""
    server = make_rcon()
    with socket.create_connection(("127.0.0.1", server.port), timeout=3) as sock:
        sock.sendall(encode_packet(1, 3, PASSWORD))
        assert read_packet(sock) == (1, 0, "")  # 认证回执是空 body

        sock.sendall(encode_packet(2, 2, "whitelist list"))
        first_id, first_type, first_body = read_packet(sock)
        second_id, second_type, second_body = read_packet(sock)

        assert (first_id, first_type, first_body) == (2, 0, "")
        assert second_id == 2
        assert "no whitelisted players" in second_body.lower()


def test_client_is_not_fooled_by_the_empty_packet(make_rcon) -> None:
    """客户端必须读到**非空**内容才算响应（行为 2）。"""
    server = make_rcon()
    whitelist = WhitelistRcon(RconClient("127.0.0.1", server.port, PASSWORD, timeout=3))
    assert whitelist.list_names() == []


def test_one_command_per_connection_and_lowercasing(make_rcon) -> None:
    """行为 1 + 行为 3：每条命令新开连接；服务端把用户名转小写。"""
    server = make_rcon()
    whitelist = WhitelistRcon(RconClient("127.0.0.1", server.port, PASSWORD, timeout=3))

    assert whitelist.list_names() == []
    assert "added probetester" in whitelist.add("ProbeTester").lower()
    assert whitelist.list_names() == ["probetester"]  # 提交的是 ProbeTester，存的是小写
    assert "already whitelisted" in whitelist.add("probetester").lower()  # 幂等回执
    assert "removed probetester" in whitelist.remove("probetester").lower()
    assert whitelist.list_names() == []

    # 6 条命令 = 6 次连接（行为 1：响应后服务端就关连接，同一连接发第二条会 EOF）
    assert server.connections == 6


def test_authentication_failure_raises(make_rcon) -> None:
    server = make_rcon(password="a-different-password")
    client = RconClient("127.0.0.1", server.port, PASSWORD, timeout=3)
    with pytest.raises(RconError):
        client.command("whitelist list")
    assert server.auth_failures == 1


def test_unreachable_port_raises() -> None:
    client = RconClient("127.0.0.1", free_port(), PASSWORD, timeout=1)
    with pytest.raises(RconError):
        client.command("whitelist list")
