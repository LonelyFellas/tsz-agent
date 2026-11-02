"""Local account commands. Run `python -m tsz_agent.auth --help`."""

import argparse
import asyncio
import json
import sys
import webbrowser
from pathlib import Path
from uuid import UUID

from tsz_agent.chatgpt_auth import AuthStore, drain_token_tasks, login
from tsz_agent.clients.tsz import TaskError
from tsz_agent.models.chatgpt import catalog


async def run(args: argparse.Namespace) -> None:
    try:
        await run_command(args)
    finally:
        await drain_token_tasks()


async def run_command(args: argparse.Namespace) -> None:
    store = AuthStore(args.auth_dir)
    if args.command.startswith("handoff-"):
        from tsz_agent import chatgpt_handoff as handoff

        result: handoff.Preparation | handoff.Receipt
        if args.command == "handoff-prepare":
            result = await handoff.prepare(store, args.account)
        elif args.command == "handoff-import":
            result = await handoff.import_account(
                store, sys.stdin.buffer.read(handoff.MAX_PACKET_BYTES + 1)
            )
        else:
            if args.handoff is None:
                raise TaskError("chatgpt_handoff_invalid")
            result = await handoff.status(
                store, args.handoff, cancel=args.command == "handoff-cancel"
            )
        print(result.model_dump_json())
        return
    if args.command == "login":

        def open_browser(url: str) -> None:
            print("请在浏览器登录 ChatGPT。", flush=True)
            if args.no_browser or not webbrowser.open(url):
                # This authorization URL contains state/PKCE but never an access/refresh/ID token.
                print(url, flush=True)

        if args.ssh_host:
            from tsz_agent.chatgpt_handoff import remote_login

            receipt = await remote_login(
                store,
                args.ssh_host,
                args.container,
                open_browser,
                client_id=args.account,
                resume=args.resume,
                cancel=args.cancel_handoff,
            )
            print(
                {
                    "complete": "已登录。回到后台点“刷新状态”。",
                    "cancelled": "已取消。",
                    "expired": "授权已过期，请重新登录。",
                    "pending": "等待授权。",
                }[receipt.status]
            )
            return
        account = await login(store, open_browser, args.account)
        print(json.dumps(account.public(), ensure_ascii=False))
        if not account.public()["plan_enabled"]:
            print("已登录，但未授权使用 ChatGPT 套餐，不能调用模型。")
        return
    data = await store.accounts()
    selected = args.account or data.active
    if args.command == "status":
        print(
            json.dumps(
                {"active": data.active, "accounts": [a.public() for a in data.accounts.values()]},
                ensure_ascii=False,
            )
        )
    elif selected is None:
        raise TaskError("chatgpt_login_required")
    elif args.command == "use":
        await store.activate(selected)
        print("已切换账号；新模型选择需重新探测，已有任务仍绑定原账号。")
    elif args.command == "logout":
        revoked = await store.logout(selected)
        print(
            "已清除本地登录凭据。"
            if revoked
            else "已清除本地凭据，远端撤销未确认；请在 https://chatgpt.com/settings/usage 断开应用。"
        )
    elif args.command == "models":
        print(
            json.dumps(
                {
                    "source": "chatgpt",
                    "provider_id": selected,
                    "models": await catalog(store, selected),
                },
                ensure_ascii=False,
            )
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="本机模型平台账号管理（当前支持 ChatGPT）")
    parser.add_argument(
        "command",
        choices=[
            "login",
            "status",
            "use",
            "logout",
            "models",
            "handoff-prepare",
            "handoff-import",
            "handoff-status",
            "handoff-cancel",
        ],
    )
    parser.add_argument("provider", choices=["chatgpt"])
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument("--account", help="已保存的 client_id；login 不指定时添加新账号")
    selection.add_argument("--resume", type=UUID, help="确认或继续未完成的 SSH 账号交接")
    selection.add_argument("--cancel-handoff", type=UUID, help="取消尚未提交的 SSH 账号交接")
    parser.add_argument("--ssh-host", help="服务器的 SSH 主机名或已有 SSH 配置名称")
    parser.add_argument("--container", default="tsz-agent-api-1", help="服务器的 Agent API 容器名")
    parser.add_argument("--handoff", type=UUID, help=argparse.SUPPRESS)
    parser.add_argument(
        "--auth-dir", type=Path, help="独立凭据目录；默认读取 TSZ_AGENT_CHATGPT_AUTH_DIR"
    )
    parser.add_argument("--no-browser", action="store_true", help="显示授权链接，由本机浏览器打开")
    args = parser.parse_args()
    if (args.ssh_host or args.resume or args.cancel_handoff) and args.command != "login":
        parser.error("SSH 交接选项仅用于 login")
    if (args.resume or args.cancel_handoff) and not args.ssh_host:
        parser.error("恢复或取消交接需要 --ssh-host")
    try:
        asyncio.run(run(args))
    except TaskError as exc:
        parser.exit(1, f"操作失败：{exc.code}\n")
    except KeyboardInterrupt:
        parser.exit(130, "已取消登录或账号操作。\n")
    except (OSError, ValueError, TimeoutError):
        parser.exit(1, "操作失败：chatgpt_auth_unavailable\n")


if __name__ == "__main__":
    main()
