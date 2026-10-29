import asyncio
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import httpx
import pytest
from test_chatgpt_auth import save_account

from tsz_agent.app import create_app
from tsz_agent.chatgpt_auth import AuthStore
from tsz_agent.chatgpt_login import ChatGPTLoginService
from tsz_agent.clients.tsz import TaskError
from tsz_agent.config import Settings
from tsz_agent.storage import Store


@pytest.mark.parametrize("configured_value,enabled", [(None, True), ("false", False)])
async def test_login_is_enabled_by_default_and_can_be_disabled(
    settings, monkeypatch, configured_value, enabled
):
    if configured_value is None:
        monkeypatch.delenv("TSZ_AGENT_CHATGPT_ENABLED", raising=False)
    else:
        monkeypatch.setenv("TSZ_AGENT_CHATGPT_ENABLED", configured_value)
    config = Settings(_env_file=None, **settings.model_dump(exclude={"chatgpt_enabled"}))
    store = Store(config.database_url.get_secret_value())
    app = create_app(config, store)
    try:
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app), base_url="http://agent.test"
            ) as http,
        ):
            response = await http.get(
                "/v1/chatgpt/accounts",
                headers={"Authorization": "Bearer " + config.api_token.get_secret_value()},
            )
            assert response.status_code == 200
            assert response.json() == {
                "configured": enabled,
                "active": None,
                "accounts": [],
                "pending_login": None,
            }
            assert config.chatgpt_auth_dir.exists() is enabled
    finally:
        await store.close()


async def test_accounts_require_service_auth_and_never_project_credentials(settings, tmp_path):
    settings = settings.model_copy(update={"chatgpt_enabled": True, "chatgpt_auth_dir": tmp_path})
    await save_account(AuthStore(tmp_path))
    store = Store(settings.database_url.get_secret_value())
    app = create_app(settings, store)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://agent.test") as http,
    ):
        assert (await http.get("/v1/chatgpt/accounts")).status_code == 401
        headers = {"Authorization": "Bearer " + settings.api_token.get_secret_value()}
        response = await http.get("/v1/chatgpt/accounts", headers=headers)
        assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
        body = response.json()
        assert body["active"] == "oaiapp_fixture" and body["accounts"][0]["plan_enabled"]
        assert "fixture-access" not in response.text and "fixture-refresh" not in response.text
        assert "subject" not in response.text and "session_id" not in response.text
        assert (
            await http.post(
                "/v1/chatgpt/login",
                headers=headers,
                json={"client_id": "oaiapp_fixture", "access_token": "must-reject"},
            )
        ).status_code == 422
    await store.close()


async def test_browser_pending_attempt_survives_page_reload_and_cancellation_closes_listener(
    tmp_path,
):
    service = ChatGPTLoginService(AuthStore(tmp_path), True)
    try:
        attempt = await service.start(None)
        pending = (await service.accounts()).pending_login
        assert pending == attempt
        with pytest.raises(TaskError, match="chatgpt_login_in_progress"):
            await service.start(None)
        with pytest.raises(TaskError, match="chatgpt_login_in_progress"):
            await service.activate("oaiapp_fixture")
        status = await service.cancel(attempt.attempt_id)
        assert status.status == "cancelled" and (await service.accounts()).pending_login is None
        callback = parse_qs(urlsplit(attempt.authorization_url).query)["redirect_uri"][0]
        async with httpx.AsyncClient(trust_env=False) as http:
            with pytest.raises(httpx.ConnectError):
                await http.get(callback)
        assert service.status(uuid4()).status == "expired"
    finally:
        await service.close()


async def test_browser_denial_is_sanitized_and_disabled_feature_does_not_read_credentials(tmp_path):
    disabled = ChatGPTLoginService(AuthStore(tmp_path / "disabled"), False)
    assert not (await disabled.accounts()).configured
    assert not disabled.store.directory.exists()
    with pytest.raises(TaskError, match="chatgpt_not_configured"):
        await disabled.start(None)
    service = ChatGPTLoginService(AuthStore(tmp_path / "enabled"), True)
    try:
        attempt = await service.start(None)
        params = parse_qs(urlsplit(attempt.authorization_url).query)
        async with httpx.AsyncClient(trust_env=False) as http:
            await http.get(
                params["redirect_uri"][0],
                params={
                    "state": params["state"][0],
                    "error": "access_denied",
                    "error_description": "private-upstream-error",
                },
            )
        await asyncio.gather(*(a.task for a in service.attempts.values()))
        status = service.status(attempt.attempt_id)
        assert status.status == "failed" and status.error == "denied"
        assert "private-upstream-error" not in status.model_dump_json()
        assert not (await service.accounts()).accounts
    finally:
        await service.close()
