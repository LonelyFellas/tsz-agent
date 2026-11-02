"""Selected-account migration over an operator's existing SSH connection."""

import asyncio
import hashlib
import json
import math
import os
import re
import shlex
import shutil
from pathlib import Path
from time import time
from typing import Any, Literal
from uuid import UUID

from pydantic import ConfigDict, SecretStr

from tsz_agent.chatgpt_auth import Account, AuthStore, CredentialHandoff, Credentials, login
from tsz_agent.chatgpt_login import ChatGPTAccount
from tsz_agent.clients.tsz import TaskError
from tsz_agent.schemas import StrictModel

MAX_PACKET_BYTES = 128_000


class Registration(StrictModel):
    client_id: str
    subject: str
    email: str
    scopes: list[str]
    session_id: str


class Preparation(StrictModel):
    version: Literal[1] = 1
    id: UUID
    host_id: str
    expires_at: float
    registration: Registration | None = None


class MigratedAccount(Account):
    model_config = ConfigDict(extra="forbid")
    access_token: SecretStr
    refresh_token: SecretStr
    id_token: SecretStr


class Packet(StrictModel):
    version: Literal[1] = 1
    id: UUID
    host_id: str
    account: MigratedAccount


class Receipt(StrictModel):
    status: Literal["pending", "complete", "cancelled", "expired"]
    account: ChatGPTAccount | None = None


class LocalRequest(StrictModel):
    host: str
    container: str
    preparation: Preparation


async def prepare(store: AuthStore, client_id: str | None) -> Preparation:
    async with store.locked():
        data = store.read()
        account = data.accounts.get(client_id) if client_id else None
        if client_id and account is None:
            raise TaskError("chatgpt_account_not_found")
        handoff = CredentialHandoff(
            expires_at=time() + 600,
            client_id=client_id,
            expected_session_id=account.session_id if account else None,
        )
        data.handoff = handoff
        store.write(data)
        return Preparation(
            id=handoff.id,
            host_id=data.host_id,
            expires_at=handoff.expires_at,
            registration=Registration.model_validate(
                account.model_dump(include=set(Registration.model_fields))
            )
            if account
            else None,
        )


def receipt(data: Credentials, identifier: UUID) -> Receipt:
    handoff = data.handoff
    if handoff is None or handoff.id != identifier:
        return Receipt(status="expired")
    if handoff.status == "complete":
        account = data.accounts.get(handoff.client_id or "")
        if account and account.session_id == handoff.imported_session_id and account.access_token:
            return Receipt(
                status="complete", account=ChatGPTAccount.model_validate(account.public())
            )
        return Receipt(status="cancelled")
    if handoff.status == "cancelled":
        return Receipt(status="cancelled")
    return Receipt(status="pending" if handoff.expires_at > time() else "expired")


async def status(store: AuthStore, identifier: UUID, *, cancel: bool = False) -> Receipt:
    async with store.locked():
        data = store.read()
        if (
            cancel
            and data.handoff
            and data.handoff.id == identifier
            and data.handoff.status == "pending"
        ):
            data.handoff.status = "cancelled"
            store.write(data)
        return receipt(data, identifier)


async def import_account(store: AuthStore, raw: bytes) -> Receipt:
    if len(raw) > MAX_PACKET_BYTES:
        raise TaskError("chatgpt_handoff_invalid")
    try:
        packet = Packet.model_validate_json(raw)
        digest = hashlib.sha256(
            json.dumps(json.loads(raw), sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
    except ValueError as exc:
        raise TaskError("chatgpt_handoff_invalid") from exc
    async with store.locked():
        data = store.read()
        handoff = data.handoff
        if handoff is None or handoff.id != packet.id or data.host_id != packet.host_id:
            raise TaskError("chatgpt_handoff_conflict")
        current = receipt(data, packet.id)
        if current.status == "complete":
            if handoff.payload_digest != digest:
                raise TaskError("chatgpt_handoff_conflict")
            return current  # Never replace credentials that the VM may already have rotated.
        if current.status != "pending":
            raise TaskError("chatgpt_handoff_expired")
        account = packet.account
        if (
            not account.subject
            or not all(
                token.get_secret_value()
                for token in (account.access_token, account.refresh_token, account.id_token)
            )
            or not math.isfinite(account.expires_at)
            or account.expires_at <= time()
            or not math.isfinite(account.earliest_refresh_at)
        ):
            raise TaskError("chatgpt_handoff_invalid")
        existing = data.accounts.get(account.client_id)
        if handoff.client_id:
            if (
                account.client_id != handoff.client_id
                or existing is None
                or existing.session_id != handoff.expected_session_id
                or existing.subject != account.subject
            ):
                raise TaskError("chatgpt_handoff_conflict")
        elif existing is not None:
            raise TaskError("chatgpt_handoff_conflict")
        data.accounts[account.client_id] = Account.model_validate(account.model_dump())
        if data.active is None:
            data.active = account.client_id
        handoff.client_id = account.client_id
        handoff.imported_session_id = account.session_id
        handoff.payload_digest = digest
        handoff.status = "complete"
        store.write(data)
        return receipt(data, packet.id)


async def ssh(
    host: str,
    container: str,
    command: str,
    *,
    payload: bytes = b"",
    identifier: UUID | None = None,
    account: str | None = None,
) -> bytes:
    if not re.fullmatch(
        r"(?:[a-zA-Z0-9_.-]+@)?[a-zA-Z0-9][a-zA-Z0-9_.-]*", host
    ) or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]*", container):
        raise TaskError("chatgpt_ssh_target_invalid")
    args = ["docker", "exec", "-i", container, "python", "-m", "tsz_agent.auth", command, "chatgpt"]
    if identifier:
        args += ["--handoff", str(identifier)]
    if account:
        args += ["--account", account]
    process = await asyncio.create_subprocess_exec(
        "ssh",
        "--",
        host,
        shlex.join(args),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        async with asyncio.timeout(30):
            output, _ = await process.communicate(payload)
    except BaseException:
        if process.returncode is None:
            process.kill()
        await process.wait()
        raise
    if process.returncode or len(output) > MAX_PACKET_BYTES:
        raise TaskError("chatgpt_ssh_unavailable")
    return output


def write_request(path: Path, request: LocalRequest) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as stream:
        stream.write(request.model_dump_json())
        stream.flush()
        os.fsync(stream.fileno())


def read_request(stage: Path) -> LocalRequest:
    fd = os.open(stage / "request.json", os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd) as stream:
        info = os.fstat(stream.fileno())
        if info.st_uid != os.getuid() or info.st_mode & 0o077 or info.st_size > MAX_PACKET_BYTES:
            raise TaskError("chatgpt_credentials_unsafe")
        return LocalRequest.model_validate_json(stream.read())


async def remote_login(
    local: AuthStore,
    host: str,
    container: str,
    open_browser: Any,
    *,
    client_id: str | None = None,
    resume: UUID | None = None,
    cancel: UUID | None = None,
) -> Receipt:
    async with local.locked():
        root = local.directory / "handoffs"
        root.mkdir(mode=0o700, exist_ok=True)
        if root.is_symlink():
            raise TaskError("chatgpt_credentials_unsafe")
    if resume or cancel:
        identifier = resume or cancel
        assert identifier is not None
        stage = root / str(identifier)
        request = read_request(stage)
        if request.host != host or request.container != container:
            raise TaskError("chatgpt_ssh_target_invalid")
        result = Receipt.model_validate_json(
            await ssh(
                host,
                container,
                "handoff-cancel" if cancel else "handoff-status",
                identifier=identifier,
            )
        )
        if result.status != "pending":
            shutil.rmtree(stage)
            return result
    else:
        prepared = Preparation.model_validate_json(
            await ssh(host, container, "handoff-prepare", account=client_id)
        )
        request = LocalRequest(host=host, container=container, preparation=prepared)
        identifier = prepared.id
        stage = root / str(identifier)
        stage.mkdir(mode=0o700)
        write_request(stage / "request.json", request)
    staged = AuthStore(stage)
    sent = False
    try:
        if resume is None:
            async with staged.locked():
                data = Credentials()
                if request.preparation.registration:
                    account = Account(**request.preparation.registration.model_dump())
                    data.accounts[account.client_id] = account
                staged.write(data)
            await login(staged, open_browser, client_id)
        async with staged.locked():
            data = staged.read()
            selected = data.accounts.get(data.active or "")
            if selected is None or selected.refresh_token is None:
                raise TaskError("chatgpt_offline_access_required")
            # Read only this staging store; the regular local account store is untouched.
            saved = json.loads(staged.path.read_bytes())["accounts"][selected.client_id]
            packet = json.dumps(
                {
                    "version": 1,
                    "id": str(identifier),
                    "host_id": request.preparation.host_id,
                    "account": saved,
                },
                sort_keys=True,
            ).encode()
        sent = True
        try:
            result = Receipt.model_validate_json(
                await ssh(host, container, "handoff-import", payload=packet)
            )
        except (TaskError, TimeoutError):
            result = Receipt.model_validate_json(
                await ssh(host, container, "handoff-status", identifier=identifier)
            )
        if result.status != "complete":
            raise TaskError("chatgpt_handoff_unconfirmed")
        shutil.rmtree(stage)
        return result
    except BaseException:
        if sent:
            print(
                f"登录结果未确认。继续：--resume {identifier}；取消：--cancel-handoff {identifier}。",
                flush=True,
            )
        else:
            try:
                await ssh(host, container, "handoff-cancel", identifier=identifier)
            except (TaskError, TimeoutError, OSError):
                pass
            shutil.rmtree(stage)
        raise
