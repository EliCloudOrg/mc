"""管理员命令行（决策 T2：**容器内 CLI 与 HTTP 管理接口都要**）。

    docker compose exec mc-whitelist python -m app.cli show-config
    docker compose exec mc-whitelist python -m app.cli list-names [--all]
    docker compose exec mc-whitelist python -m app.cli list-audit [--limit 20]
    docker compose exec mc-whitelist python -m app.cli whitelist-list
    docker compose exec mc-whitelist python -m app.cli force-remove mn_0001
    docker compose exec mc-whitelist python -m app.cli reconcile [--apply]

与 HTTP 侧走**同一套**业务函数（`whitelist.py`），不存在两套逻辑。
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from sqlalchemy import select

from .config import Settings, get_settings
from .db import init_db, session_scope
from .models import AuditLog, McName
from .rcon import RconClient, RconError, WhitelistRcon
from .whitelist import reconcile, remove_name


def _whitelist(settings: Settings) -> WhitelistRcon:
    return WhitelistRcon(
        RconClient(
            settings.rcon_host,
            settings.rcon_port,
            settings.rcon_password,
            timeout=settings.rcon_timeout,
        )
    )


def _print(payload: Any) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m app.cli", description="mc-whitelist 管理员命令")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("show-config", help="打印生效配置（不含 RCON 密码）")

    list_names = sub.add_parser("list-names", help="列出绑定（默认只看 active）")
    list_names.add_argument("--all", action="store_true", help="包含已撤回的历史条目")
    list_names.add_argument("--sub", default=None, help="只看某个 SSO sub")

    list_audit = sub.add_parser("list-audit", help="看审计日志")
    list_audit.add_argument("--limit", type=int, default=20)
    list_audit.add_argument("--action", default=None, choices=["add", "remove", "reconcile", "deny"])

    sub.add_parser("whitelist-list", help="直接打 MC 的 whitelist list（真源）")

    force_remove = sub.add_parser("force-remove", help="管理员强制移除一个条目")
    force_remove.add_argument("entry_id")

    recon = sub.add_parser("reconcile", help="与 MC 白名单对账")
    recon.add_argument("--apply", action="store_true", help="把「库有、白名单无」的缺口补回去")

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = get_settings()
    init_db(settings)

    if args.command == "show-config":
        _print(settings.log_summary())
        return 0

    if args.command == "list-audit":
        with session_scope() as session:
            statement = select(AuditLog)
            if args.action:
                statement = statement.where(AuditLog.action == args.action)
            rows = session.scalars(
                statement.order_by(AuditLog.created_at.desc(), AuditLog.id.desc()).limit(args.limit)
            ).all()
            _print([row.to_public_dict() for row in rows])
        return 0

    if args.command == "list-names":
        with session_scope() as session:
            statement = select(McName)
            if args.sub:
                statement = statement.where(McName.user_id == args.sub)
            if not args.all:
                statement = statement.where(McName.status == "active")
            rows = session.scalars(statement.order_by(McName.created_at.desc(), McName.id.desc())).all()
            _print([row.to_admin_dict() for row in rows])
        return 0

    whitelist = _whitelist(settings)

    if args.command == "whitelist-list":
        try:
            _print({"whitelist": whitelist.list_names()})
        except RconError as exc:
            print(f"RCON 失败：{exc}", file=sys.stderr)
            return 1
        return 0

    if args.command == "force-remove":
        with session_scope() as session:
            try:
                row, changed = remove_name(
                    session,
                    whitelist,
                    entry_id=args.entry_id,
                    user_id=None,  # 管理员越过归属校验
                    removed_by="admin",
                )
            except Exception as exc:  # noqa: BLE001 - CLI 要把错误打给人看
                print(f"移除失败：{exc}", file=sys.stderr)
                return 1
            _print({"entry": row.to_admin_dict(), "changed": changed})
        return 0

    if args.command == "reconcile":
        with session_scope() as session:
            try:
                report = reconcile(session, whitelist, apply=args.apply)
            except Exception as exc:  # noqa: BLE001
                print(f"对账失败：{exc}", file=sys.stderr)
                return 1
            _print(report)
        return 0

    print(f"未知命令：{args.command}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
