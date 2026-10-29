import asyncio
import json

import httpx
import pytest

from tsz_agent.clients.tsz import TaskError
from tsz_agent.models import probes
from tsz_agent.models.opencode import TOOL_PERMISSIONS, generate_text
from tsz_agent.schemas import ModelSelection

CHOICE = ModelSelection(source="opencode", provider_id="fixture", model_id="text")


@pytest.mark.parametrize("info", [{"version": "2.2.0"}, {}, []])
async def test_unverified_server_version_never_creates_an_executable_session(info):
    calls = []

    async def respond(request):
        calls.append((request.method, request.url.path))
        return httpx.Response(200, json=info)

    async with httpx.AsyncClient(
        base_url="http://fixture.test", transport=httpx.MockTransport(respond)
    ) as http:
        with pytest.raises(TaskError, match="opencode_version_unsupported"):
            await generate_text(http, CHOICE, "private input")
    assert calls == [("GET", "/api/info")]


@pytest.mark.parametrize("change", ["permissions", "model", "metadata", "id"])
async def test_unsafe_session_is_never_used_for_inference(change):
    calls = []

    async def respond(request):
        if request.url.path == "/api/info":
            return httpx.Response(200, json={"version": "2.1.4"})
        if request.url.path == "/api/plugin":
            return httpx.Response(
                200,
                json={
                    "data": [{"id": "tsz.agent.session-budget-v2", "state": {"status": "active"}}]
                },
            )
        calls.append((request.method, request.url.path))
        if request.method == "DELETE":
            return httpx.Response(204)
        body = json.loads(request.content)
        value = {"id": "ses_fixture", **body}
        if change == "permissions":
            value["permissions"] = [{"action": "*", "resource": "*", "effect": "allow"}]
        elif change == "model":
            value["model"] = {"providerID": "other", "id": "unexpected"}
        elif change == "metadata":
            value["metadata"] = {}
        else:
            value["id"] = "ses_../../unrelated"
        return httpx.Response(200, json={"data": value})

    async with httpx.AsyncClient(
        base_url="http://fixture.test", transport=httpx.MockTransport(respond)
    ) as http:
        with pytest.raises(TaskError, match="model_output_invalid"):
            await generate_text(http, CHOICE, "private input")
    assert calls == [("POST", "/api/session")] + (
        [] if change == "id" else [("DELETE", "/api/session/ses_fixture")]
    )


@pytest.mark.parametrize("stage", ["create", "generate", "delete"])
async def test_cancellation_waits_for_owned_session_cleanup(stage):
    entered, release = asyncio.Event(), asyncio.Event()
    deleted = []

    async def respond(request):
        if request.url.path == "/api/info":
            return httpx.Response(200, json={"version": "2.1.4"})
        if request.url.path == "/api/plugin":
            return httpx.Response(
                200,
                json={
                    "data": [{"id": "tsz.agent.session-budget-v2", "state": {"status": "active"}}]
                },
            )
        actual_stage = (
            "delete"
            if request.method == "DELETE"
            else "create"
            if request.url.path == "/api/session"
            else "generate"
        )
        if actual_stage == stage:
            entered.set()
            await release.wait()
        if actual_stage == "create":
            body = json.loads(request.content)
            return httpx.Response(200, json={"data": {"id": "ses_cancel", **body}})
        if actual_stage == "generate":
            return httpx.Response(200, json={"data": {"text": "OK"}})
        deleted.append(request.url.path)
        return httpx.Response(204)

    async with httpx.AsyncClient(
        base_url="http://fixture.test", transport=httpx.MockTransport(respond)
    ) as http:
        task = asyncio.create_task(generate_text(http, CHOICE, "private input"))
        await asyncio.wait_for(entered.wait(), 1)
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 1)
    assert deleted == ["/api/session/ses_cancel"]


async def test_concurrent_calls_use_separate_sessions_and_cleanup_on_failure():
    sessions, deleted, prompts = [], [], {}

    async def respond(request):
        if request.url.path == "/api/info":
            return httpx.Response(200, json={"version": "2.1.4"})
        if request.url.path == "/api/plugin":
            return httpx.Response(
                200,
                json={
                    "data": [{"id": "tsz.agent.session-budget-v2", "state": {"status": "active"}}]
                },
            )
        if request.url.path == "/api/session":
            body = json.loads(request.content)
            assert body["permissions"] == TOOL_PERMISSIONS
            identifier = f"ses_fixture{len(sessions)}"
            sessions.append(identifier)
            return httpx.Response(200, json={"data": {"id": identifier, **body}})
        identifier = request.url.path.split("/")[3]
        if request.method == "DELETE":
            deleted.append(identifier)
            return httpx.Response(204)
        prompts[identifier] = json.loads(request.content)["prompt"]
        if prompts[identifier] == "fail":
            return httpx.Response(503, json={"message": "must-not-log raw upstream secret"})
        await asyncio.sleep(0)
        return httpx.Response(200, json={"data": {"text": prompts[identifier]}})

    async with httpx.AsyncClient(
        base_url="http://fixture.test", transport=httpx.MockTransport(respond)
    ) as http:
        results = await asyncio.gather(
            generate_text(http, CHOICE, "first"),
            generate_text(http, CHOICE, "fail"),
            return_exceptions=True,
        )
    assert results[0] == "first"
    assert isinstance(results[1], TaskError) and str(results[1]) == "opencode_unavailable"
    assert len(sessions) == len(set(sessions)) == 2
    assert set(deleted) == set(sessions)
    assert sorted(prompts.values()) == ["fail", "first"]


async def test_cleanup_failure_cannot_be_reported_as_success(caplog):
    async def respond(request):
        if request.url.path == "/api/info":
            return httpx.Response(200, json={"version": "2.1.4"})
        if request.url.path == "/api/plugin":
            return httpx.Response(
                200,
                json={
                    "data": [{"id": "tsz.agent.session-budget-v2", "state": {"status": "active"}}]
                },
            )
        if request.url.path == "/api/session":
            body = json.loads(request.content)
            return httpx.Response(200, json={"data": {"id": "ses_fixture", **body}})
        if request.method == "DELETE":
            return httpx.Response(500, json={"message": "must-not-log raw upstream secret"})
        return httpx.Response(200, json={"data": {"text": "OK"}})

    async with httpx.AsyncClient(
        base_url="http://fixture.test", transport=httpx.MockTransport(respond)
    ) as http:
        with pytest.raises(TaskError, match="opencode_session_cleanup_failed"):
            await generate_text(http, CHOICE, "private input")
    assert "opencode_session_cleanup_failed" in caplog.text
    assert "must-not-log" not in caplog.text and "private input" not in caplog.text


async def test_changed_generation_protocol_invalidates_old_probe_receipts(settings, monkeypatch):
    _, old = await probes.catalog(settings)
    monkeypatch.setattr(probes, "SESSION_PROTOCOL", "other-generation-path")
    _, new = await probes.catalog(settings)
    assert old != new


@pytest.mark.parametrize("text", ["", " \n\t"])
async def test_empty_response_is_explicit_not_retried_and_session_is_removed(text):
    calls = []

    async def respond(request):
        calls.append((request.method, request.url.path))
        if request.url.path == "/api/info":
            return httpx.Response(200, json={"version": "2.1.4"})
        if request.url.path == "/api/plugin":
            return httpx.Response(
                200,
                json={
                    "data": [{"id": "tsz.agent.session-budget-v2", "state": {"status": "active"}}]
                },
            )
        if request.url.path == "/api/session":
            return httpx.Response(
                200, json={"data": {"id": "ses_empty", **json.loads(request.content)}}
            )
        if request.method == "DELETE":
            return httpx.Response(204)
        return httpx.Response(200, json={"data": {"text": text}})

    async with httpx.AsyncClient(
        base_url="http://fixture.test", transport=httpx.MockTransport(respond)
    ) as http:
        with pytest.raises(TaskError, match="model_response_empty") as error:
            await generate_text(http, CHOICE, "private input")
    assert error.value.retryable is False
    assert calls.count(("POST", "/api/session/ses_empty/generate")) == 1
    assert calls[-1] == ("DELETE", "/api/session/ses_empty")
