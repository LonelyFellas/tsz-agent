import asyncio
import json
from time import time
from uuid import uuid4

import httpx
import pytest
from pydantic import SecretStr
from test_chatgpt_auth import save_account

from tsz_agent import chatgpt_auth as auth
from tsz_agent import chatgpt_handoff as handoff
from tsz_agent.app import create_app
from tsz_agent.clients.tsz import TaskError
from tsz_agent.storage import Store


def packet(prepared, **changes):
    registration = prepared.registration
    account = {
        "client_id": registration.client_id if registration else "oaiapp_migrated",
        "subject": "user-fixture",
        "email": "fixture@example.test",
        "session_id": str(uuid4()),
        "scopes": auth.SCOPES.split(),
        "access_token": "migrated-private-access",
        "refresh_token": "migrated-private-refresh",
        "id_token": "migrated-private-identity",
        "expires_at": time() + 3600,
        "earliest_refresh_at": 0,
        **changes,
    }
    return json.dumps(
        {"version": 1, "id": str(prepared.id), "host_id": prepared.host_id, "account": account}
    ).encode()


async def test_selected_import_preserves_vm_identity_other_accounts_and_active(tmp_path):
    store = auth.AuthStore(tmp_path)
    other = await save_account(store, "oaiapp_other")
    before = await store.accounts()
    prepared = await handoff.prepare(store, None)
    assert "access_token" not in prepared.model_dump_json()
    result = await handoff.import_account(store, packet(prepared))
    after = await store.accounts()
    assert result.status == "complete" and result.account.client_id == "oaiapp_migrated"
    assert after.host_id == before.host_id
    assert after.active == other.client_id
    assert after.accounts[other.client_id] == before.accounts[other.client_id]
    assert "migrated-private" not in result.model_dump_json()
    assert store.path.stat().st_mode & 0o777 == 0o600


async def test_import_retry_does_not_overwrite_vm_rotated_tokens(tmp_path):
    store = auth.AuthStore(tmp_path)
    prepared = await handoff.prepare(store, None)
    raw = packet(prepared)
    await handoff.import_account(store, raw)
    async with store.locked():
        data = store.read()
        data.accounts["oaiapp_migrated"].refresh_token = SecretStr("vm-rotated-refresh")
        store.write(data)
    assert (await handoff.import_account(store, raw)).status == "complete"
    assert (await store.accounts()).accounts[
        "oaiapp_migrated"
    ].refresh_token.get_secret_value() == "vm-rotated-refresh"
    with pytest.raises(TaskError, match="chatgpt_handoff_conflict"):
        await handoff.import_account(store, packet(prepared))


@pytest.mark.parametrize("fault", ["host", "subject", "session", "expired", "cancelled"])
async def test_invalid_or_stale_handoff_never_changes_accounts(tmp_path, fault):
    store = auth.AuthStore(tmp_path)
    await save_account(store)
    prepared = await handoff.prepare(store, "oaiapp_fixture")
    raw = packet(prepared, subject="wrong-user" if fault == "subject" else "user-fixture")
    if fault == "host":
        body = json.loads(raw)
        body["host_id"] = "urn:uuid:" + str(uuid4())
        raw = json.dumps(body).encode()
    async with store.locked():
        data = store.read()
        if fault == "session":
            data.accounts["oaiapp_fixture"].session_id = str(uuid4())
        elif fault == "expired":
            data.handoff.expires_at = 0
        elif fault == "cancelled":
            data.handoff.status = "cancelled"
        store.write(data)
    before = store.path.read_bytes()
    with pytest.raises(TaskError):
        await handoff.import_account(store, raw)
    assert store.path.read_bytes() == before


async def test_logout_fences_pending_and_completed_import(tmp_path):
    store = auth.AuthStore(tmp_path)
    prepared = await handoff.prepare(store, None)
    raw = packet(prepared)
    await handoff.import_account(store, raw)
    async with store.locked():
        data = store.read()
        data.accounts["oaiapp_migrated"].refresh_token = None
        store.write(data)
    await store.logout("oaiapp_migrated")
    with pytest.raises(TaskError):
        await handoff.import_account(store, raw)
    assert (await handoff.status(store, prepared.id)).status == "cancelled"
    assert (await store.accounts()).accounts["oaiapp_migrated"].access_token is None


async def test_ssh_helper_recovers_lost_ack_without_leaving_local_tokens(
    tmp_path, monkeypatch, capsys
):
    vm, local = auth.AuthStore(tmp_path / "vm"), auth.AuthStore(tmp_path / "local")
    calls = []

    async def ssh(host, container, command, *, payload=b"", identifier=None, account=None):
        calls.append(command)
        if command == "handoff-prepare":
            return (await handoff.prepare(vm, account)).model_dump_json().encode()
        if command == "handoff-import":
            await handoff.import_account(vm, payload)
            raise TaskError("chatgpt_ssh_unavailable")
        return (await handoff.status(vm, identifier)).model_dump_json().encode()

    async def login(store, browser, client_id):
        return await save_account(store, "oaiapp_migrated", id_token=SecretStr("private-id-token"))

    monkeypatch.setattr(handoff, "ssh", ssh)
    monkeypatch.setattr(handoff, "login", login)
    result = await handoff.remote_login(local, "fixture-host", "tsz-agent-api-1", lambda _: None)
    assert result.status == "complete"
    assert calls == ["handoff-prepare", "handoff-import", "handoff-status"]
    assert list((local.directory / "handoffs").iterdir()) == []
    assert not local.path.exists()
    assert "fixture-access" not in capsys.readouterr().out


async def test_server_mode_exposes_setup_and_refuses_loopback_login(settings):
    config = settings.model_copy(update={"chatgpt_login_mode": "ssh"})
    store = Store(config.database_url.get_secret_value())
    app = create_app(config, store)
    try:
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app), base_url="http://agent.test"
            ) as client,
        ):
            assert (await client.get("/v1/chatgpt/setup")).status_code == 401
            headers = {"Authorization": "Bearer " + config.api_token.get_secret_value()}
            result = await client.get("/v1/chatgpt/setup", headers=headers)
            assert result.json() == {"login_method": "ssh"}
            assert result.headers["cache-control"] == "no-store"
            refused = await client.post(
                "/v1/chatgpt/login", json={"client_id": None}, headers=headers
            )
            assert refused.status_code == 422
            assert refused.json()["code"] == "chatgpt_remote_setup_required"
            assert not config.chatgpt_auth_dir.exists()
    finally:
        await store.close()


async def test_unconfirmed_import_can_resume_without_another_oauth(tmp_path, monkeypatch):
    vm, local = auth.AuthStore(tmp_path / "vm"), auth.AuthStore(tmp_path / "local")
    unavailable = True
    logins = []

    async def ssh(host, container, command, *, payload=b"", identifier=None, account=None):
        if command == "handoff-prepare":
            return (await handoff.prepare(vm, account)).model_dump_json().encode()
        if unavailable:
            raise TaskError("chatgpt_ssh_unavailable")
        if command == "handoff-import":
            return (await handoff.import_account(vm, payload)).model_dump_json().encode()
        return (await handoff.status(vm, identifier)).model_dump_json().encode()

    async def login(store, browser, client_id):
        logins.append(client_id)
        return await save_account(store, "oaiapp_migrated", id_token=SecretStr("private-id-token"))

    monkeypatch.setattr(handoff, "ssh", ssh)
    monkeypatch.setattr(handoff, "login", login)
    with pytest.raises(TaskError, match="chatgpt_ssh_unavailable"):
        await handoff.remote_login(local, "fixture-host", "tsz-agent-api-1", lambda _: None)
    identifier = (await vm.accounts()).handoff.id
    stage = local.directory / "handoffs" / str(identifier)
    assert (stage / "accounts.json").stat().st_mode & 0o777 == 0o600
    unavailable = False
    result = await handoff.remote_login(
        local, "fixture-host", "tsz-agent-api-1", lambda _: None, resume=identifier
    )
    assert result.status == "complete" and len(logins) == 1
    assert not stage.exists()


async def test_ssh_credentials_travel_only_on_standard_input(monkeypatch):
    arguments, inputs = [], []

    class Process:
        returncode = 0

        async def communicate(self, payload):
            inputs.append(payload)
            return b"{}", b""

    async def create(*args, **kwargs):
        arguments.extend(args)
        return Process()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create)
    payload = b"private-migration-fixture"
    assert (
        await handoff.ssh("fixture-host", "tsz-agent-api-1", "handoff-import", payload=payload)
        == b"{}"
    )
    assert arguments[:3] == ["ssh", "--", "fixture-host"]
    assert inputs == [payload]
    assert all("private-migration-fixture" not in value for value in arguments)
    with pytest.raises(TaskError, match="chatgpt_ssh_target_invalid"):
        await handoff.ssh("-oProxyCommand=bad", "tsz-agent-api-1", "handoff-import")
