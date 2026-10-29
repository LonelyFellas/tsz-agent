"""Bounded browser login attempts owned by the local API process."""

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import UUID, uuid4

from pydantic import Field

from tsz_agent.chatgpt_auth import AuthStore, login
from tsz_agent.clients.tsz import TaskError
from tsz_agent.schemas import StrictModel


class ChatGPTAccount(StrictModel):
    client_id: str
    email: str
    signed_in: bool
    plan_enabled: bool


class ChatGPTAccountInput(StrictModel):
    client_id: str | None = Field(
        default=None, min_length=1, max_length=200, pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]*$"
    )


class ChatGPTLogin(StrictModel):
    attempt_id: UUID
    authorization_url: str
    expires_at: datetime


class ChatGPTAccounts(StrictModel):
    configured: bool
    active: str | None = None
    accounts: list[ChatGPTAccount] = Field(default_factory=list)
    pending_login: ChatGPTLogin | None = None


class ChatGPTLoginStatus(StrictModel):
    status: Literal["pending", "complete", "failed", "expired", "cancelled"] = "pending"
    expires_at: datetime
    error: Literal["denied", "identity_invalid", "unavailable"] | None = None


class ChatGPTLogout(StrictModel):
    remote_revoked: bool


@dataclass
class Attempt:
    view: ChatGPTLoginStatus
    ready: asyncio.Future[str]
    task: asyncio.Task[None]


class ChatGPTLoginService:
    def __init__(self, store: AuthStore, enabled: bool):
        self.store, self.enabled = store, enabled
        self.attempts: dict[UUID, Attempt] = {}
        self.lock = asyncio.Lock()

    def require_enabled(self) -> None:
        if not self.enabled:
            raise TaskError("chatgpt_not_configured")

    async def accounts(self) -> ChatGPTAccounts:
        if not self.enabled:
            return ChatGPTAccounts(configured=False)
        data = await self.store.accounts()
        return ChatGPTAccounts(
            configured=True,
            active=data.active,
            accounts=[ChatGPTAccount.model_validate(a.public()) for a in data.accounts.values()],
            pending_login=next(
                (
                    ChatGPTLogin(
                        attempt_id=key,
                        authorization_url=attempt.ready.result(),
                        expires_at=attempt.view.expires_at,
                    )
                    for key, attempt in self.attempts.items()
                    if not attempt.task.done()
                    and attempt.ready.done()
                    and not attempt.ready.cancelled()
                ),
                None,
            ),
        )

    async def start(self, client_id: str | None) -> ChatGPTLogin:
        self.require_enabled()
        async with self.lock:
            # Keep only recent terminal attempts and permit only one pending authorization.
            now = datetime.now(UTC)
            self.attempts = {
                key: attempt
                for key, attempt in self.attempts.items()
                if not attempt.task.done() or attempt.view.expires_at > now
            }
            if (
                any(not attempt.task.done() for attempt in self.attempts.values())
                or len(self.attempts) >= 32
            ):
                raise TaskError("chatgpt_login_in_progress")
            identifier = uuid4()
            view = ChatGPTLoginStatus(expires_at=now + timedelta(seconds=300))
            ready: asyncio.Future[str] = asyncio.get_running_loop().create_future()

            async def run() -> None:
                try:
                    await login(self.store, lambda url: ready.set_result(url), client_id)
                    view.status = "complete"
                except asyncio.CancelledError:
                    view.status = "cancelled"
                    raise
                except TaskError as exc:
                    view.status = "expired" if exc.code == "chatgpt_login_timeout" else "failed"
                    view.error = (
                        "denied"
                        if exc.code == "chatgpt_login_denied"
                        else "identity_invalid"
                        if exc.code
                        in {
                            "chatgpt_identity_invalid",
                            "chatgpt_account_mismatch",
                            "chatgpt_client_mismatch",
                        }
                        else "unavailable"
                    )
                except Exception:
                    # Credentials, callback codes and upstream exception text never enter reports.
                    view.status, view.error = "failed", "unavailable"
                finally:
                    if not ready.done():
                        ready.cancel()

            task = asyncio.create_task(run())
            self.attempts[identifier] = Attempt(view, ready, task)
            try:
                url = await asyncio.wait_for(asyncio.shield(ready), timeout=5)
            except (TimeoutError, asyncio.CancelledError) as exc:
                request_cancelled = (
                    isinstance(exc, asyncio.CancelledError) and not ready.cancelled()
                )
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                if request_cancelled:
                    raise
                raise TaskError("chatgpt_login_unavailable") from exc
            return ChatGPTLogin(
                attempt_id=identifier, authorization_url=url, expires_at=view.expires_at
            )

    def require_idle(self) -> None:
        self.require_enabled()
        if any(not attempt.task.done() for attempt in self.attempts.values()):
            raise TaskError("chatgpt_login_in_progress")

    async def activate(self, client_id: str) -> ChatGPTAccounts:
        async with self.lock:
            self.require_idle()
            await self.store.activate(client_id)
            return await self.accounts()

    async def logout(self, client_id: str) -> ChatGPTLogout:
        async with self.lock:
            self.require_idle()
            return ChatGPTLogout(remote_revoked=await self.store.logout(client_id))

    def status(self, identifier: UUID) -> ChatGPTLoginStatus:
        self.require_enabled()
        attempt = self.attempts.get(identifier)
        if attempt is None:
            # Restarted API processes cannot resume an in-memory authorization transaction.
            return ChatGPTLoginStatus(status="expired", expires_at=datetime.now(UTC))
        return attempt.view

    async def cancel(self, identifier: UUID) -> ChatGPTLoginStatus:
        self.require_enabled()
        attempt = self.attempts.get(identifier)
        if attempt is not None and not attempt.task.done():
            attempt.task.cancel()
            await asyncio.gather(attempt.task, return_exceptions=True)
        return self.status(identifier)

    async def close(self) -> None:
        for attempt in self.attempts.values():
            attempt.task.cancel()
        await asyncio.gather(
            *(attempt.task for attempt in self.attempts.values()), return_exceptions=True
        )
