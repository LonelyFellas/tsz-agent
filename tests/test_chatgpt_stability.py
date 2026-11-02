"""Exercise ChatGPT session recovery with synthetic rotating credentials only."""

import asyncio
from time import time
from urllib.parse import parse_qs

import httpx
import pytest
from test_chatgpt_auth import save_account

from tsz_agent import chatgpt_auth as auth
from tsz_agent.clients.tsz import TaskError


async def test_cancelled_refresh_waiter_does_not_lose_rotated_credentials(tmp_path, monkeypatch):
    store = auth.AuthStore(tmp_path)
    account = await save_account(store)
    async with store.locked():
        data = store.read()
        data.accounts[account.client_id].expires_at = 0
        store.write(data)
    issued, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def respond(request):
        values = parse_qs(request.content.decode())
        calls.append(values["refresh_token"][0])
        if len(calls) > 1:
            return httpx.Response(400, json={"error": "invalid_grant"})
        issued.set()
        await release.wait()
        return httpx.Response(
            200,
            json={
                "access_token": "rotated-access",
                "refresh_token": "rotated-refresh",
                "token_type": "Bearer",
                "expires_in": 3600,
            },
        )

    monkeypatch.setattr(
        auth, "http_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(respond))
    )
    waiter = asyncio.create_task(store.token(account.client_id))
    await asyncio.wait_for(issued.wait(), timeout=1)
    waiter.cancel()
    try:
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(waiter, timeout=1)
    finally:
        release.set()

    token = await asyncio.wait_for(auth.AuthStore(tmp_path).token(account.client_id), timeout=1)
    assert token.get_secret_value() == "rotated-access"
    assert len(calls) == 1
    saved = (await store.accounts()).accounts[account.client_id]
    assert saved.refresh_token.get_secret_value() == "rotated-refresh"
    assert saved.session_id == account.session_id


@pytest.mark.parametrize("has_refresh_token", [True, False])
async def test_unavailable_early_refresh_can_use_valid_access_token(
    tmp_path, monkeypatch, has_refresh_token
):
    store = auth.AuthStore(tmp_path)
    account = await save_account(store)
    async with store.locked():
        data = store.read()
        data.accounts[account.client_id].expires_at = time() + 30
        data.accounts[account.client_id].earliest_refresh_at = (
            time() + 20 if has_refresh_token else 0
        )
        if not has_refresh_token:
            data.accounts[account.client_id].refresh_token = None
        store.write(data)

    def forbidden():
        pytest.fail("a deferred refresh must not send an OAuth request")

    monkeypatch.setattr(auth, "http_client", forbidden)
    assert (await store.token(account.client_id)).get_secret_value() == "fixture-access"


async def test_expired_access_with_deferred_refresh_remains_retryable(tmp_path, monkeypatch):
    store = auth.AuthStore(tmp_path)
    account = await save_account(store)
    async with store.locked():
        data = store.read()
        data.accounts[account.client_id].expires_at = 0
        data.accounts[account.client_id].earliest_refresh_at = time() + 20
        store.write(data)
    with pytest.raises(TaskError, match="chatgpt_refresh_not_ready") as error:
        await store.token(account.client_id)
    assert error.value.retryable
    assert (await store.accounts()).accounts[account.client_id].refresh_token is not None


async def test_refresh_rate_limit_is_retryable_and_keeps_credentials(tmp_path, monkeypatch):
    store = auth.AuthStore(tmp_path)
    account = await save_account(store)
    async with store.locked():
        data = store.read()
        data.accounts[account.client_id].expires_at = 0
        store.write(data)
    monkeypatch.setattr(
        auth,
        "http_client",
        lambda: httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(429, json={"error": "rate_limit_exceeded"})
            )
        ),
    )
    with pytest.raises(TaskError, match="chatgpt_refresh_failed") as error:
        await store.token(account.client_id)
    assert error.value.retryable
    saved = (await store.accounts()).accounts[account.client_id]
    assert saved.refresh_token.get_secret_value() == "fixture-refresh"


async def test_catalog_timeout_keeps_refresh_and_next_catalog_recovers(
    settings, tmp_path, monkeypatch
):
    from tsz_agent.models import chatgpt, probes

    store = auth.AuthStore(tmp_path)
    account = await save_account(store)
    async with store.locked():
        data = store.read()
        data.accounts[account.client_id].expires_at = 0
        store.write(data)
    release = asyncio.Event()
    calls = []

    async def respond(request):
        calls.append(request)
        await release.wait()
        return httpx.Response(
            200,
            json={
                "access_token": "catalog-rotated-access",
                "refresh_token": "catalog-rotated-refresh",
                "token_type": "Bearer",
                "expires_in": 3600,
            },
        )

    async def catalog(store, client_id):
        await store.token(client_id)
        return [{"model_id": "fixture-model", "name": "Fixture"}]

    monkeypatch.setattr(
        auth, "http_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(respond))
    )
    monkeypatch.setattr(chatgpt, "catalog", catalog)
    monkeypatch.setattr(probes, "CHATGPT_CATALOG_TIMEOUT_SECONDS", 0.02)
    settings = settings.model_copy(update={"chatgpt_auth_dir": tmp_path, "chatgpt_enabled": True})
    try:
        models, _ = await probes.catalog(settings)
        assert calls and not any(model.source == "chatgpt" for model in models)
    finally:
        release.set()
    assert (await store.token(account.client_id)).get_secret_value() == "catalog-rotated-access"
    models, _ = await probes.catalog(settings)
    assert any(model.source == "chatgpt" and model.model_id == "fixture-model" for model in models)
    assert len(calls) == 1


async def test_cancelled_refresh_failure_releases_lock_and_keeps_credentials(tmp_path, monkeypatch):
    store = auth.AuthStore(tmp_path)
    account = await save_account(store)
    async with store.locked():
        data = store.read()
        data.accounts[account.client_id].expires_at = 0
        store.write(data)
    entered, release = asyncio.Event(), asyncio.Event()

    async def respond(request):
        entered.set()
        await release.wait()
        return httpx.Response(503, json={"error": "temporary_failure"})

    monkeypatch.setattr(
        auth, "http_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(respond))
    )
    waiter = asyncio.create_task(store.token(account.client_id))
    await asyncio.wait_for(entered.wait(), timeout=1)
    waiter.cancel()
    try:
        with pytest.raises(asyncio.CancelledError):
            await waiter
    finally:
        release.set()
    saved = await asyncio.wait_for(auth.AuthStore(tmp_path).accounts(), timeout=1)
    assert saved.accounts[account.client_id].refresh_token.get_secret_value() == "fixture-refresh"
