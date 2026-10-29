import asyncio
import base64
import hashlib
import json
from time import time
from urllib.parse import parse_qs, urlsplit

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from pydantic import SecretStr

from tsz_agent import chatgpt_auth as auth
from tsz_agent.clients.tsz import TaskError


@pytest.fixture
def signing_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def signed_token(key, nonce, client_id="oaiapp_fixture", **changes):
    claims = {
        "iss": auth.ISSUER,
        "sub": "user-fixture",
        "aud": client_id,
        "iat": int(time()),
        "exp": int(time()) + 3600,
        "nonce": nonce,
        "email": "fixture@example.test",
        **changes,
    }
    return jwt.encode(claims, key, algorithm="RS256", headers={"kid": "fixture"})


def jwks(key):
    return {
        "keys": [
            {**json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key())), "kid": "fixture"}
        ]
    }


async def save_account(store, client_id="oaiapp_fixture", **changes):
    async with store.locked():
        data = store.read()
        account = auth.Account(
            client_id=client_id,
            subject="user-fixture",
            email="fixture@example.test",
            access_token=SecretStr("fixture-access"),
            refresh_token=SecretStr("fixture-refresh"),
            scopes=auth.SCOPES.split(),
            expires_at=time() + 3600,
            **changes,
        )
        data.accounts[client_id] = account
        data.active = client_id
        store.write(data)
        return account


async def test_login_real_loopback_pkce_and_protected_account_storage(
    tmp_path, monkeypatch, signing_key
):
    store = auth.AuthStore(tmp_path / "chatgpt")
    transactions, redirects, exchanges, callbacks = [], [], [], []

    def open_browser(url):
        params = {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}
        transactions.append(params)
        assert url.startswith(auth.AUTHORIZE + "?")
        assert params["scope"] == auth.SCOPES
        assert params["resource"] == auth.RESOURCE
        assert params["ext_agent_host_id"].startswith("urn:uuid:")
        if len(transactions) == 1:
            assert params["client_id"] == "dynamic_agent_client"
            assert params["agent_name_hint"] == "tsz-agent"
        else:
            assert params["client_id"] == "oaiapp_fixture" and "agent_name_hint" not in params
            assert params["ext_agent_host_id"] == transactions[0]["ext_agent_host_id"]
        redirects.append(params["redirect_uri"])

        async def callback():
            async with httpx.AsyncClient(trust_env=False) as http:
                wrong = await http.get(
                    params["redirect_uri"], params={"state": "wrong", "code": "bad"}
                )
                assert wrong.status_code == 400
                response = await http.get(
                    params["redirect_uri"],
                    params={
                        "state": params["state"],
                        "code": "fixture-code",
                        "client_id": "oaiapp_fixture",
                    },
                )
                assert response.status_code == 200 and "fixture-code" not in response.text

        callbacks.append(asyncio.create_task(callback()))

    async def respond(request):
        if request.url.path.endswith("openid-configuration"):
            return httpx.Response(
                200, json={"issuer": auth.ISSUER, "jwks_uri": auth.ISSUER + "/jwks"}
            )
        if request.url.path == "/jwks":
            return httpx.Response(200, json=jwks(signing_key))
        values = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        exchanges.append(values)
        assert values["client_id"] == "oaiapp_fixture"
        assert values["redirect_uri"] == redirects[-1]
        challenge = (
            base64.urlsafe_b64encode(hashlib.sha256(values["code_verifier"].encode()).digest())
            .decode()
            .rstrip("=")
        )
        assert transactions[-1]["code_challenge"] == challenge
        return httpx.Response(
            200,
            json={
                "access_token": "fixture-access",
                "refresh_token": "fixture-refresh",
                "id_token": signed_token(signing_key, transactions[-1]["nonce"]),
                "token_type": "Bearer",
                "expires_in": 3600,
                "scope": auth.SCOPES,
            },
        )

    monkeypatch.setattr(
        auth, "http_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(respond))
    )
    first = await auth.login(store, open_browser)
    second = await auth.login(store, open_browser, first.client_id)
    await asyncio.gather(*callbacks)
    assert first.subject == second.subject == "user-fixture" and len(exchanges) == 2
    assert transactions[0]["state"] != transactions[1]["state"]
    assert store.directory.stat().st_mode & 0o777 == 0o700
    assert store.path.stat().st_mode & 0o777 == 0o600
    assert "fixture-access" in store.path.read_text()
    public = json.dumps(second.public()) + repr(second)
    assert "fixture-access" not in public and "fixture-refresh" not in public
    assert (await store.accounts()).active == "oaiapp_fixture"
    with pytest.raises(httpx.ConnectError):
        async with httpx.AsyncClient(trust_env=False) as http:
            await http.get(redirects[-1])


@pytest.mark.parametrize(
    "change",
    [{"iss": "https://evil.test"}, {"aud": "other-client"}, {"nonce": "wrong"}, {"exp": 1}],
)
async def test_oidc_rejects_untrusted_identity(signing_key, change):
    async def respond(request):
        return httpx.Response(
            200,
            json={"issuer": auth.ISSUER, "jwks_uri": auth.ISSUER + "/jwks"}
            if request.url.path.endswith("openid-configuration")
            else jwks(signing_key),
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        token = (
            signed_token(signing_key, "nonce", **change)
            if "nonce" not in change
            else signed_token(signing_key, "wrong")
        )
        with pytest.raises(TaskError, match="chatgpt_identity_invalid"):
            await auth.verify_identity(http, token, "oaiapp_fixture", "nonce")


async def test_refresh_is_serialized_across_store_instances(tmp_path, monkeypatch):
    store = auth.AuthStore(tmp_path)
    account = await save_account(store)
    async with store.locked():
        data = store.read()
        data.accounts[account.client_id].expires_at = 0
        store.write(data)
    calls = []

    async def respond(request):
        calls.append(parse_qs(request.content.decode()))
        await asyncio.sleep(0.1)
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
    values = await asyncio.gather(
        store.token(account.client_id), auth.AuthStore(tmp_path).token(account.client_id)
    )
    assert all(v.get_secret_value() == "rotated-access" for v in values)
    assert len(calls) == 1
    assert calls[0]["resource"] == [auth.RESOURCE] and "scope" not in calls[0]
    assert (await store.accounts()).accounts[
        account.client_id
    ].refresh_token.get_secret_value() == "rotated-refresh"


@pytest.mark.parametrize(
    "status,code,cleared", [(400, "invalid_grant", True), (503, "temporary_failure", False)]
)
async def test_refresh_failure_preserves_registration(tmp_path, monkeypatch, status, code, cleared):
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
            transport=httpx.MockTransport(lambda _: httpx.Response(status, json={"error": code}))
        ),
    )
    with pytest.raises(
        TaskError, match="chatgpt_login_required" if cleared else "chatgpt_refresh_failed"
    ):
        await store.token(account.client_id)
    saved = (await store.accounts()).accounts[account.client_id]
    assert saved.client_id == account.client_id and (saved.access_token is None) == cleared


async def test_logout_revokes_only_selected_registration_and_retains_host(tmp_path, monkeypatch):
    store = auth.AuthStore(tmp_path)
    await save_account(store)
    await save_account(store, "oaiapp_second")
    before = await store.accounts()
    requests = []

    async def respond(request):
        if request.method == "GET":
            return httpx.Response(200, json={"revocation_endpoint": auth.ISSUER + "/revoke"})
        requests.append(parse_qs(request.content.decode()))
        return httpx.Response(200)

    monkeypatch.setattr(
        auth, "http_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(respond))
    )
    assert await store.logout("oaiapp_fixture")
    after = await store.accounts()
    assert after.host_id == before.host_id and after.active == "oaiapp_second"
    assert after.accounts["oaiapp_fixture"].access_token is None
    assert after.accounts["oaiapp_second"].access_token is not None
    assert requests[0]["client_id"] == ["oaiapp_fixture"]


@pytest.mark.parametrize("eventual_success", [True, False])
async def test_logout_retries_transient_revocation_and_reports_residual_remote_session(
    tmp_path, monkeypatch, eventual_success
):
    store = auth.AuthStore(tmp_path)
    await save_account(store)
    calls = []

    def respond(request):
        if request.method == "GET":
            return httpx.Response(200, json={"revocation_endpoint": auth.ISSUER + "/revoke"})
        calls.append(request)
        if len(calls) == 1:
            raise httpx.ConnectError("sensitive provider error")
        return httpx.Response(200 if eventual_success else 503)

    monkeypatch.setattr(
        auth, "http_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(respond))
    )
    assert await store.logout("oaiapp_fixture") is eventual_success
    assert len(calls) == (2 if eventual_success else 3)
    account = (await store.accounts()).accounts["oaiapp_fixture"]
    assert account.access_token is None and account.refresh_token is None


async def test_credentials_refuse_symlinks_and_readable_token_files(tmp_path):
    store = auth.AuthStore(tmp_path / "private")
    await save_account(store)
    store.path.chmod(0o644)
    with pytest.raises(TaskError, match="chatgpt_credentials_unsafe"):
        await store.accounts()
    store.path.unlink()
    target = tmp_path / "outside"
    target.write_text("do not read")
    store.path.symlink_to(target)
    with pytest.raises(TaskError, match="chatgpt_credentials_unsafe"):
        await store.accounts()
    assert target.read_text() == "do not read"


async def test_logout_remote_deadline_still_clears_local_tokens(tmp_path, monkeypatch):
    store = auth.AuthStore(tmp_path)
    await save_account(store)
    entered = asyncio.Event()

    async def respond(request):
        if request.method == "GET":
            return httpx.Response(200, json={"revocation_endpoint": auth.ISSUER + "/revoke"})
        entered.set()
        await asyncio.sleep(1)
        return httpx.Response(200)

    monkeypatch.setattr(auth, "REVOCATION_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(
        auth, "http_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(respond))
    )
    assert await store.logout("oaiapp_fixture") is False
    assert entered.is_set()
    account = (await store.accounts()).accounts["oaiapp_fixture"]
    assert account.access_token is None and account.refresh_token is None


async def test_busy_credential_lock_returns_bounded_retryable_error(tmp_path, monkeypatch):
    owner, waiter = auth.AuthStore(tmp_path), auth.AuthStore(tmp_path)
    monkeypatch.setattr(auth, "LOCK_TIMEOUT_SECONDS", 0.01)
    async with owner.locked():
        with pytest.raises(TaskError, match="chatgpt_credentials_busy") as error:
            await waiter.accounts()
        assert error.value.retryable
    assert not (await waiter.accounts()).accounts


@pytest.mark.parametrize("failure", ["denied", "client_mismatch", "identity_mismatch", "unsigned"])
async def test_failed_reauthorization_never_replaces_active_credentials(
    tmp_path, monkeypatch, signing_key, failure
):
    store = auth.AuthStore(tmp_path)
    await save_account(store)
    before = store.path.read_bytes()
    pending = {}
    callbacks = []

    def browser(url):
        pending.update({k: v[0] for k, v in parse_qs(urlsplit(url).query).items()})

        async def callback():
            values = {"state": pending["state"], "code": "fixture-code"}
            if failure == "denied":
                values["error"] = "access_denied"
            if failure == "client_mismatch":
                values["client_id"] = "oaiapp_wrong"
            async with httpx.AsyncClient(trust_env=False) as http:
                await http.get(pending["redirect_uri"], params=values)

        callbacks.append(asyncio.create_task(callback()))

    def respond(request):
        if request.url.path.endswith("openid-configuration"):
            return httpx.Response(
                200, json={"issuer": auth.ISSUER, "jwks_uri": auth.ISSUER + "/jwks"}
            )
        if request.url.path == "/jwks":
            return httpx.Response(200, json=jwks(signing_key))
        token = signed_token(signing_key, pending["nonce"], sub="other-user")
        if failure == "unsigned":
            token = jwt.encode(
                {"sub": "user-fixture"}, key="", algorithm="none", headers={"kid": "fixture"}
            )
        return httpx.Response(
            200,
            json={
                "access_token": "wrong-access",
                "id_token": token,
                "token_type": "Bearer",
                "expires_in": 3600,
                "scope": auth.SCOPES,
            },
        )

    monkeypatch.setattr(
        auth, "http_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(respond))
    )
    expected = {
        "denied": "chatgpt_login_denied",
        "client_mismatch": "chatgpt_client_mismatch",
        "identity_mismatch": "chatgpt_account_mismatch",
        "unsigned": "chatgpt_identity_invalid",
    }[failure]
    with pytest.raises(TaskError, match=expected):
        await auth.login(store, browser, "oaiapp_fixture")
    await asyncio.gather(*callbacks)
    assert store.path.read_bytes() == before
