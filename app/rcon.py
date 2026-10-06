"""最小 RCON 客户端（Valve RCON / Minecraft Java 版）。

规格与证据：`docs/mc-whitelist.md` §5（全部来自线上实测）。四条必须照做的行为：

1. **一条连接只跑一条命令**：MC 在响应后会主动关闭连接，同一连接里连发第二条会读到 EOF；
2. 每条命令**可能先回一个空包、再回真实内容**（两个包可能带同一个 id）→ 读到空包不能当响应；
3. 用户名一律被服务端转成小写（本模块不参与，只如实返回响应文本）；
4. 离线模式下 UUID 形态不稳定（v5 与随机 v4 并存）→ **绝不构造 UUID**，本模块不碰它。

判定原则（§5.3）：**不信 add/remove 的回执，以 `whitelist list` 为准。**

注入防线（§5.4 / §10 第 1 条）：用户名在这里**再校验一次**正则，只有 `^[A-Za-z0-9_]{3,16}$`
才可能被拼进命令。即"上层忘了校验"，RCON 层也不会把任意字符串送进 MC 控制台。
"""

from __future__ import annotations

import logging
import re
import socket
import struct
import threading
import time
from contextlib import closing

logger = logging.getLogger(__name__)

TYPE_RESPONSE = 0
TYPE_COMMAND = 2
TYPE_AUTH = 3

AUTH_REQUEST_ID = 1
COMMAND_REQUEST_ID = 2
AUTH_FAILED_ID = -1

MAX_PAYLOAD = 4096
_MIN_PACKET_LENGTH = 10  # 4(id) + 4(type) + 2(结尾空字节)

# 进程内串行化：MC 的 RCON 监听本身是串行的，并发只会排队 + 超时（§5.5）。
# 用 RLock：`whitelist.py` 会持有它跨越「list → add → list」整段事务。
command_lock = threading.RLock()

# MC 官方用户名规则；只有匹配它的字符串才允许进入 RCON 命令
SAFE_NAME_RE = re.compile(r"\A[A-Za-z0-9_]{3,16}\Z")


class RconError(RuntimeError):
    """RCON 层面的任何失败：连不上、认证不过、包不合法、响应无法解析。"""


def encode_packet(request_id: int, packet_type: int, body: str) -> bytes:
    payload = body.encode("utf-8") + b"\x00\x00"
    return struct.pack("<iii", len(payload) + 8, request_id, packet_type) + payload


def _read_exactly(sock: socket.socket, size: int) -> bytes:
    chunks = b""
    while len(chunks) < size:
        try:
            chunk = sock.recv(size - len(chunks))
        except OSError as exc:  # 含 socket.timeout
            raise RconError(f"读取 RCON 响应失败：{exc}") from exc
        if not chunk:
            raise RconError("RCON 连接被对方关闭")
        chunks += chunk
    return chunks


def read_packet(sock: socket.socket) -> tuple[int, int, str]:
    """读一个完整包，返回 ``(request_id, packet_type, body)``。"""
    (length,) = struct.unpack("<i", _read_exactly(sock, 4))
    if length < _MIN_PACKET_LENGTH or length > MAX_PAYLOAD:
        raise RconError(f"RCON 包长度异常：{length}")
    data = _read_exactly(sock, length)
    request_id, packet_type = struct.unpack("<ii", data[:8])
    body = data[8:-2].decode("utf-8", errors="replace")
    return request_id, packet_type, body


class RconClient:
    """一条命令一条连接（行为 1）；连接、认证、执行、关闭都在一次 ``command()`` 里完成。"""

    def __init__(self, host: str, port: int, password: str, *, timeout: float = 5.0) -> None:
        self.host = host
        self.port = int(port)
        self.password = password
        self.timeout = float(timeout)

    # ------------------------------------------------------------------ 公开
    @property
    def endpoint(self) -> str:
        return f"{self.host}:{self.port}"

    def command(self, command: str) -> str:
        """执行一条控制台命令并返回响应文本。**不自动重试**（§5.5）。"""
        with command_lock:
            return self._execute(command)

    # ------------------------------------------------------------------ 内部
    def _connect(self) -> socket.socket:
        try:
            sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        except OSError as exc:
            raise RconError(f"连接 RCON {self.endpoint} 失败：{exc}") from exc
        sock.settimeout(self.timeout)
        return sock

    def _authenticate(self, sock: socket.socket) -> None:
        sock.sendall(encode_packet(AUTH_REQUEST_ID, TYPE_AUTH, self.password))
        request_id, _packet_type, _body = read_packet(sock)
        if request_id == AUTH_FAILED_ID:
            # 认证失败时服务端回 id = -1（不是关闭连接）
            raise RconError("RCON 认证失败（RCON_PASSWORD 不对）")

    def _execute(self, command: str) -> str:
        with closing(self._connect()) as sock:
            try:
                self._authenticate(sock)
                sock.sendall(encode_packet(COMMAND_REQUEST_ID, TYPE_COMMAND, command))
                return self._read_response(sock)
            except OSError as exc:
                raise RconError(f"RCON 通信失败：{exc}") from exc

    def _read_response(self, sock: socket.socket) -> str:
        """读到**非空**内容为止（行为 2）；对方响应后关闭连接属正常结束。"""
        deadline = time.monotonic() + self.timeout
        received = False
        last = ""
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return last
            sock.settimeout(remaining)
            try:
                _request_id, _packet_type, body = read_packet(sock)
            except RconError as exc:
                if not received:
                    raise RconError(f"读取 RCON 响应失败：{exc}") from exc
                return last
            received = True
            if body:
                return body
            last = body


# --------------------------------------------------------------- 白名单语义层

_NO_PLAYERS_MARKER = "no whitelisted players"
_PLAYERS_MARKER = "player(s):"


def parse_whitelist_list(response: str) -> list[str]:
    """解析 `whitelist list` 的响应（§5.3 的两种形态）。名字统一小写。"""
    text = (response or "").strip()
    lowered = text.lower()
    if _NO_PLAYERS_MARKER in lowered:
        return []
    index = lowered.find(_PLAYERS_MARKER)
    if index < 0:
        raise RconError(f"无法解析 whitelist list 响应：{text!r}")
    tail = text[index + len(_PLAYERS_MARKER) :]
    names = [item.strip().lower() for item in tail.split(",")]
    return [name for name in names if name]


def guard_name(name: str) -> str:
    """把名字钉死在 `^[A-Za-z0-9_]{3,16}$` 之内，否则拒绝拼进命令。"""
    if not SAFE_NAME_RE.match(name or ""):
        raise RconError(f"拒绝把不合法的用户名送进 RCON：{name!r}")
    return name


class WhitelistRcon:
    """把「白名单」语义包在 RCON 之上：list / add / remove。

    这三个方法是**唯一**允许把用户名拼进控制台命令的地方，且都过 `guard_name`。
    """

    def __init__(self, client: RconClient) -> None:
        self._client = client

    @property
    def endpoint(self) -> str:
        return self._client.endpoint

    def list_names(self) -> list[str]:
        """真源：`whitelist list`。"""
        return parse_whitelist_list(self._client.command("whitelist list"))

    def add(self, name: str) -> str:
        return self._client.command(f"whitelist add {guard_name(name)}")

    def remove(self, name: str) -> str:
        return self._client.command(f"whitelist remove {guard_name(name)}")

    def ping(self) -> list[str]:
        """健康检查用：一次 list 既验证认证，也验证命令通道。"""
        return self.list_names()
