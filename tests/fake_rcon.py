"""假 RCON 服务器：复刻 `docs/mc.md` §5.2 的四条线上实测行为。

用它跑契约测试（§9.1），**不依赖真实 MC、不装任何新镜像**。四条行为：

1. 一条连接只跑一条命令，响应后**主动关闭连接**；
2. 每条命令**先回一个空包、再回真实内容**（`tests/test_rcon.py` 会证明客户端不会被空包骗到）；
3. 用户名被转成**小写**（`Added probetester to the whitelist`）；
4. `whitelist add` 对不存在的用户名也"成功"（离线模式不校验账号存在）→ 输入约束只能靠自己。

额外提供两个故障注入模式，用于验证"不信回执"这条铁律：

* ``mode="lying_add"``：`add` 回执说成功，但**实际没写进白名单** → 服务必须靠回读发现并返回 503；
* ``password`` 不匹配 → 认证失败（回 id = -1）。
"""

from __future__ import annotations

import socket
import struct
import threading

TYPE_RESPONSE = 0
TYPE_COMMAND = 2
TYPE_AUTH = 3

AUTH_FAILED_ID = -1


def encode_packet(request_id: int, packet_type: int, body: str) -> bytes:
    payload = body.encode("utf-8") + b"\x00\x00"
    return struct.pack("<iii", len(payload) + 8, request_id, packet_type) + payload


def read_packet(conn: socket.socket) -> tuple[int, int, str]:
    header = _read_exactly(conn, 4)
    (length,) = struct.unpack("<i", header)
    data = _read_exactly(conn, length)
    request_id, packet_type = struct.unpack("<ii", data[:8])
    return request_id, packet_type, data[8:-2].decode("utf-8", errors="replace")


def _read_exactly(conn: socket.socket, size: int) -> bytes:
    chunks = b""
    while len(chunks) < size:
        chunk = conn.recv(size - len(chunks))
        if not chunk:
            raise OSError("connection closed")
        chunks += chunk
    return chunks


class FakeRconServer:
    def __init__(self, password: str = "test-rcon-password", *, mode: str = "normal") -> None:
        self.password = password
        self.mode = mode
        self.players: list[str] = []
        self.commands: list[str] = []
        self.auth_failures = 0
        self.connections = 0

        self._stopping = False
        self._sock: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self.port = 0

    # ------------------------------------------------------------- 生命周期
    def start(self) -> "FakeRconServer":
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", 0))
        sock.listen(16)
        self._sock = sock
        self.port = sock.getsockname()[1]
        self._stopping = False
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stopping = True
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None

    # ------------------------------------------------------------------ 断言用
    @property
    def add_commands(self) -> list[str]:
        return [c for c in self.commands if c.lower().startswith("whitelist add")]

    @property
    def remove_commands(self) -> list[str]:
        return [c for c in self.commands if c.lower().startswith("whitelist remove")]

    def reset(self) -> None:
        self.players.clear()
        self.commands.clear()
        self.auth_failures = 0
        self.connections = 0

    # ------------------------------------------------------------------ 内部
    def _serve(self) -> None:
        while not self._stopping and self._sock is not None:
            try:
                conn, _address = self._sock.accept()
            except OSError:
                return
            self.connections += 1
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn: socket.socket) -> None:
        """一条连接：AUTH → 一条命令 → 关闭（行为 1）。"""
        with conn:
            try:
                request_id, packet_type, body = read_packet(conn)
            except OSError:
                return

            if packet_type == TYPE_AUTH:
                if body != self.password:
                    self.auth_failures += 1
                    conn.sendall(encode_packet(AUTH_FAILED_ID, TYPE_RESPONSE, ""))
                    return
                conn.sendall(encode_packet(request_id, TYPE_RESPONSE, ""))
                try:
                    request_id, packet_type, body = read_packet(conn)
                except OSError:
                    return

            if packet_type != TYPE_COMMAND:
                return

            self.commands.append(body)
            response = self._respond(body)
            # 行为 2：先空包、再真实内容
            conn.sendall(encode_packet(request_id, TYPE_RESPONSE, ""))
            if self.mode != "no_response":
                conn.sendall(encode_packet(request_id, TYPE_RESPONSE, response))
        # with 退出即关闭连接（行为 1）

    def _respond(self, command: str) -> str:
        text = command.strip()
        lowered = text.lower()

        if lowered == "whitelist list":
            if not self.players:
                return "There are no whitelisted players"
            return f"There are {len(self.players)} whitelisted player(s): " + ", ".join(self.players)

        if lowered.startswith("whitelist add "):
            # 行为 3：服务端把名字转小写
            name = text[len("whitelist add ") :].strip().lower()
            if name in self.players:
                return "Player is already whitelisted"
            if self.mode == "lying_add":
                # 回执说成功，但什么都没发生 —— 服务必须靠回读发现
                return f"Added {name} to the whitelist"
            self.players.append(name)
            return f"Added {name} to the whitelist"

        if lowered.startswith("whitelist remove "):
            name = text[len("whitelist remove ") :].strip().lower()
            if name not in self.players:
                return "Player is not whitelisted"
            self.players.remove(name)
            return f"Removed {name} from the whitelist"

        return f"Unknown command: {text}"


def free_port() -> int:
    """拿一个当前没人监听的端口（用于模拟"RCON 连不上"）。"""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port
