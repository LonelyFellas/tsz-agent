"""Local account commands. Run `python -m tsz_agent.auth --help`."""

import argparse
import asyncio
import json
import webbrowser
from pathlib import Path

from tsz_agent.chatgpt_auth import AuthStore, login
from tsz_agent.clients.tsz import TaskError
from tsz_agent.models.chatgpt import catalog


async def run(args: argparse.Namespace) -> None:
    store = AuthStore(args.auth_dir)
    if args.command == "login":

        def open_browser(url: str) -> None:
            print("Continue with ChatGPT：请在浏览器中完成登录和套餐使用授权。", flush=True)
            if args.no_browser or not webbrowser.open(url):
                # This authorization URL contains state/PKCE but never an access/refresh/ID token.
                print(url, flush=True)

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
    parser.add_argument("command", choices=["login", "status", "use", "logout", "models"])
    parser.add_argument("provider", choices=["chatgpt"])
    parser.add_argument("--account", help="已保存的 client_id；login 不指定时添加新账号")
    parser.add_argument(
        "--auth-dir", type=Path, help="独立凭据目录；默认读取 TSZ_AGENT_CHATGPT_AUTH_DIR"
    )
    parser.add_argument("--no-browser", action="store_true", help="显示授权链接，由本机浏览器打开")
    args = parser.parse_args()
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
