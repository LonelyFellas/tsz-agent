"""Regression cases from the delivery review; credentials and providers are synthetic."""

import argparse
import asyncio
import json
from datetime import timedelta
from urllib.parse import parse_qs
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import func, select
from test_assistant_stability import (
    USAGE,
    ProviderStream,
    frame,
    install_provider,
    streaming_response,
)
from test_chatgpt_auth import save_account
from test_chatgpt_handoff import packet
from test_lexicon_assistant import FakeLedger

from tsz_agent import auth as cli
from tsz_agent import chatgpt_auth as auth
from tsz_agent import chatgpt_handoff as handoff
from tsz_agent import lexicon_assistant as assistant
from tsz_agent import worker
from tsz_agent.app import create_app
from tsz_agent.clients.tsz import TaskError
from tsz_agent.models.probes import ModelProbe, ModelProbeReport
from tsz_agent.schemas import CreateRun
from tsz_agent.storage import Run


@pytest.mark.parametrize("method", ["local", "ssh"])
async def test_expired_old_refresh_does_not_abort_reauthorization(tmp_path, monkeypatch, method):
    store = auth.AuthStore(tmp_path)
    account = await save_account(store)
    async with store.locked():
        data = store.read()
        data.accounts[account.client_id].expires_at = 0
        store.write(data)

    def respond(request):
        if parse_qs(request.content.decode())["grant_type"] == ["refresh_token"]:
            return httpx.Response(400, json={"error": "invalid_grant"})
        return httpx.Response(
            200,
            json={
                "access_token": "reauthorized-access",
                "refresh_token": "reauthorized-refresh",
                "id_token": "reauthorized-identity",
                "token_type": "Bearer",
                "expires_in": 3600,
                "scope": auth.SCOPES,
            },
        )

    monkeypatch.setattr(
        auth, "http_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(respond))
    )

    async def expire_old_credentials():
        with pytest.raises(TaskError, match="chatgpt_login_required"):
            await store.token(account.client_id)

    if method == "ssh":
        prepared = await handoff.prepare(store, account.client_id)
        await expire_old_credentials()
        receipt = await handoff.import_account(store, packet(prepared))
        assert receipt.status == "complete"
    else:
        callbacks = []

        async def verify(*args):
            return {"sub": account.subject, "email": account.email}

        async def complete(url):
            await expire_old_credentials()
            query = parse_qs(url.split("?", 1)[1])
            async with httpx.AsyncClient(trust_env=False) as http:
                response = await http.get(
                    query["redirect_uri"][0],
                    params={
                        "state": query["state"][0],
                        "code": "synthetic-code",
                        "client_id": account.client_id,
                    },
                )
                assert response.status_code == 200

        monkeypatch.setattr(auth, "verify_identity", verify)
        try:
            result = await auth.login(
                store,
                lambda url: callbacks.append(asyncio.create_task(complete(url))),
                account.client_id,
            )
            assert result.access_token.get_secret_value() == "reauthorized-access"
        finally:
            await asyncio.gather(*callbacks)


async def test_unknown_local_account_does_not_cancel_pending_handoff(tmp_path):
    store = auth.AuthStore(tmp_path)
    account = await save_account(store)
    prepared = await handoff.prepare(store, account.client_id)
    before = store.path.read_bytes()
    with pytest.raises(TaskError, match="chatgpt_account_not_found"):
        await auth.login(store, lambda _: pytest.fail("must not open authorization"), "missing")
    assert store.path.read_bytes() == before
    assert (await handoff.import_account(store, packet(prepared))).status == "complete"


@pytest.mark.parametrize("runtime", ["api", "cli", "worker"])
async def test_runtime_shutdown_waits_for_rotated_credentials(
    settings, tmp_path, monkeypatch, runtime
):
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
        return httpx.Response(
            200,
            json={
                "access_token": "shutdown-access",
                "refresh_token": "shutdown-refresh",
                "token_type": "Bearer",
                "expires_in": 3600,
            },
        )

    monkeypatch.setattr(
        auth, "http_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(respond))
    )

    async def cancelled_waiter():
        waiter = asyncio.create_task(store.token(account.client_id))
        await entered.wait()
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter

    class RuntimeStore:
        async def ready(self):
            pass

        async def close(self):
            pass

    if runtime == "api":
        app = create_app(settings, RuntimeStore())
        context = app.router.lifespan_context(app)
        await context.__aenter__()
        await cancelled_waiter()
        closing = asyncio.create_task(context.__aexit__(None, None, None))
    elif runtime == "cli":

        async def catalog(*args):
            await cancelled_waiter()
            return []

        monkeypatch.setattr(cli, "catalog", catalog)
        closing = asyncio.create_task(
            cli.run(
                argparse.Namespace(command="models", auth_dir=tmp_path, account=account.client_id)
            )
        )
    else:

        async def run_once(*args):
            await cancelled_waiter()
            raise asyncio.CancelledError

        monkeypatch.setattr(worker, "Settings", lambda: settings)
        monkeypatch.setattr(worker, "Store", lambda _: RuntimeStore())
        monkeypatch.setattr(worker, "run_once", run_once)
        closing = asyncio.create_task(worker.main())
    try:
        await asyncio.wait_for(entered.wait(), 1)
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(asyncio.shield(closing), 0.03)
    finally:
        release.set()
        await asyncio.gather(closing, return_exceptions=True)
        saved = (await store.accounts()).accounts[account.client_id]
    assert saved.refresh_token.get_secret_value() == "shutdown-refresh"


@pytest.mark.integration
@pytest.mark.parametrize("offset", [-65, 65])
@pytest.mark.parametrize("dispatch_first", [True, False])
async def test_probe_invalidation_uses_one_clock(store, offset, dispatch_first):
    run = await store.create(CreateRun(external_request_id=uuid4()), uuid4())
    lease = await store.claim(60, 3)
    async with store.sessions() as session:
        at = await session.scalar(select(func.clock_timestamp()))
    model = ModelProbe(
        source="direct",
        provider_id="deepseek",
        model_id=run.usage.model,
        name=run.usage.model,
        enabled=True,
        status="available",
        checked_at=at + timedelta(seconds=offset),
    )
    report = ModelProbeReport(batch_id=uuid4(), state="running", models=[model])
    await store.start_probe(
        {
            "context": "review-clock-fixture",
            "deadline": (at + timedelta(seconds=300)).isoformat(),
            "report": report.model_dump(mode="json"),
        }
    )

    async def dispatch():
        await store.reserve_model_call(run.id, lease.lease_token, uuid4(), 100, 6, 6000)

    if dispatch_first:
        await dispatch()
    await store.record_probe(report.batch_id, model)
    await store.finish_probe(report.batch_id, interrupted=False)
    if not dispatch_first:
        await dispatch()
    await store.invalidate_probe(run.usage.model, at, (run.id, lease.lease_token))
    saved = await store.probe_packet()
    assert saved["report"]["models"][0]["status"] == (
        "available" if dispatch_first else "unavailable"
    )


@pytest.mark.integration
async def test_locked_exhausted_job_does_not_block_claiming_other_work(store):
    exhausted = await store.create(CreateRun(external_request_id=uuid4()), uuid4())
    claimed = await store.claim(60, 1)
    async with store.sessions.begin() as session:
        row = await session.get(Run, claimed.id)
        row.lease_until = await session.scalar(select(func.clock_timestamp())) - timedelta(
            seconds=1
        )
    runnable = await store.create(CreateRun(external_request_id=uuid4()), uuid4())
    async with store.sessions.begin() as locker:
        await locker.get(Run, exhausted.id, with_for_update=True)
        picked = await asyncio.wait_for(store.claim(60, 1), 0.5)
        assert picked is not None and picked.id == runnable.id
    assert (await store.get(exhausted.id)).state == "running"
    await store.claim(60, 1)
    assert (await store.get(exhausted.id)).state == "failed"


async def test_heartbeat_stream_obeys_overall_deadline_and_settles(settings, monkeypatch):
    class Heartbeats(ProviderStream):
        async def __aiter__(self):
            yield frame({}, usage=USAGE)
            while True:
                await asyncio.sleep(0.005)
                yield b": heartbeat\n\n"

    stream = Heartbeats([])
    install_provider(monkeypatch, lambda _: streaming_response(stream))
    from pydantic import SecretStr

    config = settings.model_copy(
        update={"deepseek_api_key": SecretStr("synthetic-review-key"), "request_timeout_seconds": 1}
    )
    ledger = FakeLedger()

    async def collect():
        return [
            json.loads(event)
            async for event in assistant.stream_step(
                config,
                assistant.AssistantStepInput.model_validate(
                    {"messages": [{"role": "user", "content": "检查词条"}]}
                ),
                ledger,
            )
        ]

    events = await asyncio.wait_for(collect(), 2)
    assert events[-1]["event"] == "failed"
    assert stream.closed and len(ledger.settled) == 1
    assert ledger.settled[0][:2] == (50, 10)


async def test_worker_sigterm_waits_for_refresh_in_real_process(tmp_path):
    import signal
    import sys

    script = r"""
import asyncio
import contextlib
import sys
from pathlib import Path
from types import SimpleNamespace
import httpx
from pydantic import SecretStr
from tsz_agent import worker
from tsz_agent import chatgpt_auth as auth

root = Path(sys.argv[1])
store = auth.AuthStore(root)
entered = asyncio.Event()

async def respond(request):
    entered.set()
    while not (root / "release").exists():
        await asyncio.sleep(0.01)
    return httpx.Response(200, json={"access_token": "signal-new-access", "refresh_token": "signal-new-refresh", "token_type": "Bearer", "expires_in": 3600})

auth.http_client = lambda: httpx.AsyncClient(transport=httpx.MockTransport(respond))
worker.Settings = lambda: SimpleNamespace(deepseek_api_key=None, opencode_server_url=None, chatgpt_enabled=True, database_url=SecretStr("synthetic-unused"), tsz_base_url="http://tsz.test", tsz_service_token=SecretStr("synthetic-service-token"))

class RuntimeStore:
    async def ready(self):
        pass
    async def close(self):
        pass

worker.Store = lambda _: RuntimeStore()

async def run_once(*args):
    waiter = asyncio.create_task(store.token("oaiapp_signal"))
    await entered.wait()
    waiter.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await waiter
    print("READY", flush=True)
    await asyncio.Event().wait()

worker.run_once = run_once

async def run():
    async with store.locked():
        data = auth.Credentials(active="oaiapp_signal", accounts={"oaiapp_signal": auth.Account(client_id="oaiapp_signal", subject="signal-fixture", access_token=SecretStr("signal-old-access"), refresh_token=SecretStr("signal-old-refresh"), scopes=auth.SCOPES.split(), expires_at=0)})
        store.write(data)
    await worker.main()

try:
    asyncio.run(run())
except asyncio.CancelledError:
    pass
"""
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-I",
        "-c",
        script,
        str(tmp_path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        assert await asyncio.wait_for(process.stdout.readline(), 10) == b"READY\n"
        process.send_signal(signal.SIGTERM)
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(process.wait(), 0.1)
        (tmp_path / "release").touch()
        _, error = await asyncio.wait_for(process.communicate(), 5)
        assert process.returncode == 0, error.decode()
        saved = await auth.AuthStore(tmp_path).accounts()
        assert (
            saved.accounts["oaiapp_signal"].refresh_token.get_secret_value() == "signal-new-refresh"
        )
    finally:
        (tmp_path / "release").touch()
        if process.returncode is None:
            process.kill()
            await process.wait()
