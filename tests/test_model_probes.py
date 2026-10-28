from datetime import UTC, datetime
from uuid import uuid4

import httpx
import pytest

from tsz_agent.app import create_app
from tsz_agent.clients.tsz import TaskError
from tsz_agent.models import probes
from tsz_agent.models.opencode import OpenCodeModel
from tsz_agent.models.probes import ModelProbe, ProbeService
from tsz_agent.schemas import CreateRun, ModelSelection
from tsz_agent.storage import Store


@pytest.mark.integration
async def test_catalog_enabled_does_not_authorize_unprobed_model_selection(
    store, settings, monkeypatch
):
    async def catalog_only(*args):
        # OpenCode lists this model as enabled; no successful inference was observed.
        return None

    monkeypatch.setattr(OpenCodeModel, "validate_selection", catalog_only)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(create_app(settings, store)), base_url="http://agent.test"
    ) as client:
        response = await client.put(
            "/v1/model-selection",
            headers={"Authorization": "Bearer " + settings.api_token.get_secret_value()},
            json={
                "source": "opencode",
                "provider_id": "opencode",
                "model_id": "listed-but-unusable",
            },
        )
    assert response.status_code == 422
    run = await store.create(CreateRun(external_request_id=uuid4()), uuid4())
    assert run.usage.model == "deepseek-flash"


def model(number=1):
    return ModelProbe(
        source="opencode",
        provider_id="test-provider",
        model_id=f"model-{number}",
        name=f"Model {number}",
        enabled=True,
    )


@pytest.mark.parametrize(
    "failure,expected",
    [
        (503, "request_failed"),
        ("timeout", "timeout"),
        ("empty", "invalid_response"),
        ("good", None),
    ],
)
async def test_probe_observes_inference_and_never_stores_raw_output(
    settings, monkeypatch, failure, expected
):
    calls = []

    async def respond(request):
        import json

        calls.append(json.loads(request.content))
        if failure == "timeout":
            raise httpx.ReadTimeout("must-not-log raw provider error")
        if failure == 503:
            return httpx.Response(
                503, json={"message": "must-not-log API key or provider exception"}
            )
        return httpx.Response(200, json={"data": {"text": "" if failure == "empty" else "OK"}})

    monkeypatch.setattr(
        probes,
        "client",
        lambda _: httpx.AsyncClient(
            base_url="http://opencode.test", transport=httpx.MockTransport(respond)
        ),
    )
    result = await probes.probe_one(settings, model())
    assert result.status == ("available" if expected is None else "unavailable")
    assert result.error == expected and result.cost is None
    assert result.duration_ms >= 0 and result.checked_at is not None
    assert calls == [
        {"prompt": probes.PROMPT, "model": {"providerID": "test-provider", "id": "model-1"}}
    ]
    assert "must-not-log" not in result.model_dump_json()


@pytest.mark.integration
async def test_duplicate_batch_is_one_dispatch_and_results_survive_restart(
    store, settings, monkeypatch
):
    import asyncio

    models = [model(i) for i in range(5)]
    context = ["credentials-v1"]

    async def catalog(_):
        return models, context[0]

    monkeypatch.setattr(probes, "catalog", catalog)
    active = 0
    maximum = 0
    calls = 0
    entered = asyncio.Event()
    release = asyncio.Event()

    async def check(settings, choice):
        nonlocal active, maximum, calls
        calls += 1
        active += 1
        maximum = max(maximum, active)
        if active == 2:
            entered.set()
        await release.wait()
        active -= 1
        return choice.model_copy(
            update={"status": "available", "checked_at": datetime.now(UTC), "duration_ms": 1.0}
        )

    monkeypatch.setattr(probes, "probe_one", check)
    first, second = ProbeService(settings, store), ProbeService(settings, store)
    reports = await asyncio.gather(first.start(), second.start())
    assert reports[0].batch_id == reports[1].batch_id
    await asyncio.wait_for(entered.wait(), timeout=2)
    assert calls == 2 and maximum == 2
    release.set()
    await asyncio.gather(*first.tasks, *second.tasks)
    assert calls == len(models) and maximum == 2
    fresh = Store(settings.database_url.get_secret_value())
    try:
        service = ProbeService(settings, fresh)
        result = await service.report()
        assert result.state == "completed" and all(m.status == "available" for m in result.models)
        choice = ModelSelection(source="opencode", provider_id="test-provider", model_id="model-1")
        assert await service.require_available(choice) == "credentials-v1"
        await fresh.set_model_selection(choice, probe_context="credentials-v1")
        context[0] = "credentials-v2"
        assert all(m.status == "unverified" for m in (await service.report()).models)
        with pytest.raises(TaskError, match="model_not_verified"):
            await service.require_available(choice)
    finally:
        await fresh.close()


@pytest.mark.integration
async def test_interruption_stops_further_calls_and_is_visible(store, settings, monkeypatch):
    import asyncio

    models = [model(i) for i in range(5)]

    async def catalog(_):
        return models, "context"

    monkeypatch.setattr(probes, "catalog", catalog)
    entered = asyncio.Event()
    calls = 0

    async def check(settings, choice):
        nonlocal calls
        calls += 1
        if calls == 2:
            entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(probes, "probe_one", check)
    service = ProbeService(settings, store)
    await service.start()
    await asyncio.wait_for(entered.wait(), timeout=2)
    await service.close()
    result = await service.report()
    assert calls == 2 and result.state == "interrupted"
    assert all(m.status == "unavailable" and m.error == "interrupted" for m in result.models)


@pytest.mark.integration
async def test_saving_rechecks_proof_and_late_runtime_failure_cannot_revoke_new_probe(
    store, settings, monkeypatch
):
    import asyncio

    async def catalog(_):
        return [model()], "context"

    async def check(_, choice):
        return choice.model_copy(update={"status": "available", "checked_at": datetime.now(UTC)})

    monkeypatch.setattr(probes, "catalog", catalog)
    monkeypatch.setattr(probes, "probe_one", check)
    before = datetime.now(UTC)
    service = ProbeService(settings, store)
    await service.start()
    await asyncio.gather(*service.tasks)
    choice = ModelSelection(source="opencode", provider_id="test-provider", model_id="model-1")
    await store.invalidate_probe(choice.usage_model(), before, None)
    assert await service.require_available(choice) == "context"
    await store.invalidate_probe(choice.usage_model(), datetime.now(UTC), None)
    with pytest.raises(TaskError, match="model_not_verified"):
        await store.set_model_selection(choice, probe_context="context")
    assert (await store.model_selection()).source == "direct"
    assert (await service.report()).models[0].error == "runtime_failed"


@pytest.mark.integration
async def test_probe_api_is_authenticated_async_and_hides_private_context(
    store, settings, monkeypatch
):
    import asyncio

    async def catalog(_):
        return [model()], "private-fingerprint"

    async def check(_, choice):
        return choice.model_copy(
            update={
                "status": "unavailable",
                "error": "request_failed",
                "http_status": 503,
                "checked_at": datetime.now(UTC),
            }
        )

    monkeypatch.setattr(probes, "catalog", catalog)
    monkeypatch.setattr(probes, "probe_one", check)
    app = create_app(settings, store)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://agent.test"
        ) as client,
    ):
        assert (await client.post("/v1/model-probes")).status_code == 401
        headers = {"Authorization": "Bearer " + settings.api_token.get_secret_value()}
        response = await client.post("/v1/model-probes", headers=headers)
        assert response.status_code == 202 and response.json()["state"] == "running"
        for _ in range(50):
            response = await client.get("/v1/model-probes", headers=headers)
            if response.json()["state"] == "completed":
                break
            await asyncio.sleep(0.01)
        body = response.json()
        assert body["state"] == "completed" and body["models"][0]["status"] == "unavailable"
        assert "private-fingerprint" not in response.text and body["models"][0]["cost"] is None
        assert (
            await client.put(
                "/v1/model-selection",
                headers=headers,
                json={"source": "opencode", "provider_id": "test-provider", "model_id": "model-1"},
            )
        ).status_code == 422


async def test_active_connection_order_changes_context_without_storing_credential_details(
    settings, monkeypatch
):
    from pydantic import AnyHttpUrl, SecretStr

    settings = settings.model_copy(
        update={
            "opencode_server_url": AnyHttpUrl("http://127.0.0.1:4096"),
            "opencode_server_password": SecretStr("test-opencode-" + "x" * 32),
        }
    )
    connections = [{"type": "credential", "id": "first"}, {"type": "credential", "id": "second"}]

    async def native(http, method, path):
        if path == "/api/model":
            return [
                {
                    "providerID": "test-provider",
                    "id": "model-1",
                    "name": "Test",
                    "enabled": True,
                    "capabilities": {"output": ["text"]},
                }
            ]
        return [{"id": "test-provider", "connections": list(connections)}]

    monkeypatch.setattr(probes, "request", native)
    models, first = await probes.catalog(settings)
    connections.reverse()
    _, second = await probes.catalog(settings)
    assert first != second
    assert all("credential" not in m.model_dump_json() for m in models)


@pytest.mark.integration
async def test_credentials_change_stops_undispatched_probes(store, settings, monkeypatch):
    import asyncio

    models = [model(i) for i in range(5)]
    context = ["old"]

    async def catalog(_):
        return models, context[0]

    entered = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def check(_, choice):
        nonlocal calls
        calls += 1
        if calls == 2:
            entered.set()
        await release.wait()
        return choice.model_copy(update={"status": "available", "checked_at": datetime.now(UTC)})

    monkeypatch.setattr(probes, "catalog", catalog)
    monkeypatch.setattr(probes, "probe_one", check)
    service = ProbeService(settings, store)
    await service.start()
    await asyncio.wait_for(entered.wait(), timeout=2)
    context[0] = "new"
    release.set()
    await asyncio.gather(*service.tasks)
    report = await service.report()
    assert (
        calls == 2
        and report.state == "idle"
        and all(m.status == "unverified" for m in report.models)
    )


@pytest.mark.integration
async def test_runtime_failure_uses_actual_dispatch_time_after_probe(store, settings, monkeypatch):
    import asyncio

    async def catalog(_):
        return [model()], "context"

    async def check(_, choice):
        return choice.model_copy(update={"status": "available", "checked_at": datetime.now(UTC)})

    monkeypatch.setattr(probes, "catalog", catalog)
    monkeypatch.setattr(probes, "probe_one", check)
    choice = ModelSelection(source="opencode", provider_id="test-provider", model_id="model-1")
    await store.set_model_selection(choice)
    run = await store.create(CreateRun(external_request_id=uuid4()), uuid4())
    await store.set_model_selection(ModelSelection())
    lease = await store.claim(60, 3)
    process_started = datetime.now(UTC)
    service = ProbeService(settings, store)
    await service.start()
    await asyncio.gather(*service.tasks)
    await store.reserve_model_call(run.id, lease.lease_token, uuid4(), 16, 4, 6000)
    await store.invalidate_probe(choice.usage_model(), process_started, (run.id, lease.lease_token))
    assert (await service.report()).models[0].status == "unavailable"
    assert (await store.model_selection()).source == "direct"


@pytest.mark.integration
async def test_context_change_keeps_batch_owned_until_inflight_requests_exit(
    store, settings, monkeypatch
):
    import asyncio

    models = [model(i) for i in range(5)]
    context = ["old"]
    active = 0
    calls = 0
    entered = asyncio.Event()
    changed_seen = asyncio.Event()
    slow = asyncio.Event()
    fast = asyncio.Event()
    early_finish = []

    async def catalog(_):
        if context[0] == "new":
            changed_seen.set()
        return models, context[0]

    async def check(_, choice):
        nonlocal active, calls
        active += 1
        calls += 1
        if active == 2:
            entered.set()
        try:
            await (slow if choice.model_id == "model-0" else fast).wait()
            return choice.model_copy(
                update={"status": "available", "checked_at": datetime.now(UTC)}
            )
        finally:
            active -= 1

    original = store.finish_probe

    async def finish(*args, **kwargs):
        if active:
            early_finish.append(active)
        return await original(*args, **kwargs)

    monkeypatch.setattr(store, "finish_probe", finish)
    monkeypatch.setattr(probes, "catalog", catalog)
    monkeypatch.setattr(probes, "probe_one", check)
    first, second = ProbeService(settings, store), ProbeService(settings, store)
    try:
        old = await first.start()
        await asyncio.wait_for(entered.wait(), timeout=2)
        context[0] = "new"
        fast.set()
        await asyncio.wait_for(changed_seen.wait(), timeout=2)
        assert not early_finish
        duplicate = await second.start()
        assert duplicate.batch_id == old.batch_id and calls == 2
        slow.set()
        await asyncio.gather(*first.tasks, *second.tasks)
        assert not early_finish
    finally:
        slow.set()
        fast.set()
        await first.close()
        await second.close()


@pytest.mark.integration
async def test_old_lease_failure_does_not_use_new_lease_dispatch_time(store, settings, monkeypatch):
    import asyncio

    from sqlalchemy import text

    async def catalog(_):
        return [model()], "context"

    async def check(_, choice):
        return choice.model_copy(update={"status": "available", "checked_at": datetime.now(UTC)})

    monkeypatch.setattr(probes, "catalog", catalog)
    monkeypatch.setattr(probes, "probe_one", check)
    choice = ModelSelection(source="opencode", provider_id="test-provider", model_id="model-1")
    await store.set_model_selection(choice)
    run = await store.create(CreateRun(external_request_id=uuid4()), uuid4())
    old = await store.claim(60, 3)
    started = datetime.now(UTC)
    await store.reserve_model_call(run.id, old.lease_token, uuid4(), 16, 4, 6000)
    service = ProbeService(settings, store)
    await service.start()
    await asyncio.gather(*service.tasks)
    async with store.sessions.begin() as session:
        await session.execute(
            text("UPDATE agent_runs SET lease_until=now()-interval '1 second' WHERE id=:id"),
            {"id": run.id},
        )
    new = await store.claim(60, 3)
    assert old.lease_token != new.lease_token
    await store.reserve_model_call(run.id, new.lease_token, uuid4(), 16, 4, 6000)
    await store.invalidate_probe(choice.usage_model(), started, (run.id, old.lease_token))
    assert (await service.report()).models[0].status == "available"
