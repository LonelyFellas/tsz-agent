import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import delete, text, update

from tsz_agent.app import create_app
from tsz_agent.storage import Conflict
from tsz_agent.supervisor import candidates, dictionary_materials
from tsz_agent.supervisor import workflow as runtime
from tsz_agent.supervisor.dictionary_sources.cambridge import CambridgeEntry
from tsz_agent.supervisor.dictionary_sources.oxford import OxfordEntry
from tsz_agent.supervisor.dictionary_sources.youdao import YoudaoSummary
from tsz_agent.supervisor.lexicon import (
    Grammar,
    GrammarSubPosOption,
    Input,
    Material,
    Proposals,
    Scope,
    SenseContext,
    View,
    validate_rows,
)
from tsz_agent.supervisor.runtime import Step, SupervisorRun
from tsz_agent.supervisor.tools import search_dictionaries


def request(sections):
    return Input(
        scope=Scope(
            run_id=uuid4(),
            entry_id=uuid4(),
            actor_id=uuid4(),
            base_revision=2,
            headword="tomorrow",
            target="en-GB",
            medium="zh-Hans",
            existing_pos=["noun"],
            examples=[],
            senses=[],
            sections=sections,
            catalog={"noun": ["base"]},
            snapshot_id="a" * 64,
        ),
        materials=[],
    )


def test_grammar_keeps_only_sub_pos_from_the_current_basic_pos():
    scope = request(["grammar"]).scope
    scope.grammar_sub_pos = {
        "noun": [GrammarSubPosOption(code="N-COUNT", name_zh="可数名词", name_en="countable noun")],
        "verb": [
            GrammarSubPosOption(code="V-I", name_zh="不及物动词", name_en="intransitive verb")
        ],
    }
    valid = {"pos": "noun", "pattern": "a tomorrow", "sub_pos": "N-COUNT"}
    wrong_pos = {"pos": "noun", "pattern": "the tomorrow", "sub_pos": "V-I"}
    without_recommendation = {"pos": "noun", "pattern": "tomorrow"}
    assert validate_rows(
        "grammar", {"items": [valid, wrong_pos, without_recommendation]}, scope, Proposals()
    ) == [valid, {"pos": "noun", "pattern": "the tomorrow"}, without_recommendation]
    assert validate_rows(
        "grammar", {"items": [valid]}, request(["grammar"]).scope, Proposals()
    ) == [{"pos": "noun", "pattern": "a tomorrow"}]


def test_relation_candidates_follow_the_editor_template():
    scope = request(["relations"]).scope
    scope.headword = "chat"
    scope.senses = [SenseContext(pos="noun", gloss="轻松、非正式的交谈")]
    synonym = {"sense": 0, "kind": "synonym", "word": "small talk", "gloss": "闲谈", "score": 85.25}
    derivative = {**synonym, "kind": "derivative", "word": "chatty", "gloss": "健谈的", "score": 90}
    raw = {
        "items": [
            {**synonym, "word": "闲聊", "gloss": "非正式交谈"},
            {**derivative, "word": "chats", "gloss": "闲聊（复数形式）"},
            synonym,
            {**synonym, "word": "Small  Talk", "gloss": "寒暄"},
            {**synonym, "word": "conversation", "score": 100},
            {**synonym, "word": "gossip", "score": 80.123},
            {**synonym, "word": "gossip", "sense": 1},
            derivative,
            {**derivative, "gloss": "轻松随意的"},
        ]
    }
    assert validate_rows("relations", raw, scope, Proposals()) == [
        synonym,
        {**synonym, "word": "conversation", "score": 100},
        derivative,
        {**derivative, "gloss": "轻松随意的"},
    ]


@pytest.mark.parametrize(
    ("pattern", "recommendation", "expected"),
    [
        ("run something + adv./prep.", "V-T-COMPLEX", None),
        ("run something + from home", "V-T-COMPLEX", None),
        ("run + object + object complement", "V-T-COMPLEX", None),
        (
            "run + object + object complement (e.g. run the company from home)",
            "V-T-COMPLEX",
            None,
        ),
        (
            "run + object + object complement (e.g. run [the company] [from home])",
            "V-T-COMPLEX",
            None,
        ),
        (
            "run + object + object complement (e.g. run [the company] [yesterday])",
            "V-T-COMPLEX",
            None,
        ),
        (
            "run + object + object complement (e.g. run [the company] [fast])",
            "V-T-COMPLEX",
            None,
        ),
        (
            "run + object + object complement (e.g. run [the engine] [hot])",
            "V-T-COMPLEX",
            "V-T-COMPLEX",
        ),
        (
            "run + object + object complement (e.g. run [the business] [into the ground])",
            "V-T-COMPLEX",
            "V-T-COMPLEX",
        ),
        ("run something + from home", "V-T-MONO", "V-T-MONO"),
        ("run + adv./prep.", "V-I", "V-I"),
    ],
)
def test_grammar_requires_an_explicit_object_complement(pattern, recommendation, expected):
    scope = request(["grammar"]).scope.model_copy(
        update={"headword": "run", "catalog": {"verb": ["base"]}, "existing_pos": ["verb"]}
    )
    scope.grammar_sub_pos = {
        "verb": [
            GrammarSubPosOption(code=code, name_zh=code, name_en=code)
            for code in ("V-T-COMPLEX", "V-T-MONO", "V-I")
        ],
    }
    rows = validate_rows(
        "grammar",
        {"items": [{"pos": "verb", "pattern": pattern, "sub_pos": recommendation}]},
        scope,
        Proposals(),
    )
    assert len(rows) == 1 and rows[0]["pattern"] == pattern
    assert rows[0].get("sub_pos") == expected


@pytest.mark.parametrize(
    ("headword", "example", "verified"),
    [
        ("go", "go [straight] [to sleep]", False),
        ("play", "play [the game] [clean]", False),
        ("make", "make [them] [happy]", True),
        ("elect", "elect [her] [president]", True),
    ],
)
def test_complex_transitive_requires_a_verified_complete_example(headword, example, verified):
    scope = request(["grammar"]).scope.model_copy(
        update={"headword": headword, "catalog": {"verb": ["base"]}, "existing_pos": ["verb"]}
    )
    scope.grammar_sub_pos = {
        "verb": [
            GrammarSubPosOption(
                code="V-T-COMPLEX", name_zh="复合及物", name_en="complex transitive"
            )
        ]
    }
    pattern = f"{headword} + object + object complement (e.g. {example})"
    row = {"pos": "verb", "pattern": pattern}
    proposed = {**row, "sub_pos": "V-T-COMPLEX"}
    assert validate_rows("grammar", {"items": [proposed]}, scope, Proposals()) == [
        proposed if verified else row
    ]


async def test_grammar_replay_removes_old_ambiguous_complex_transitive_recommendation(
    settings, monkeypatch
):
    settings = settings.model_copy(
        update={"supervisor_enabled": True, "deepseek_api_key": SecretStr("local-test-only")}
    )
    body = request(["grammar"])
    body.scope.headword = "run"
    body.scope.existing_pos = ["verb"]
    body.scope.catalog = {"verb": ["base"]}
    body.scope.grammar_sub_pos = {
        "verb": [
            GrammarSubPosOption(
                code="V-T-COMPLEX", name_zh="复合及物", name_en="complex transitive"
            )
        ],
    }

    async def replay(*args, **kwargs):
        return View(
            run_id=body.scope.run_id,
            base_revision=body.scope.base_revision,
            snapshot_id=body.scope.snapshot_id,
            done=True,
            steps=[Step(section="grammar", state="completed")],
            proposals=Proposals(
                grammar=[
                    Grammar(pos="verb", pattern="run something + adv./prep.", sub_pos="V-T-COMPLEX")
                ]
            ),
            sources=[],
        )

    monkeypatch.setattr(runtime, "advance", replay)
    transport = httpx.ASGITransport(create_app(settings))
    async with httpx.AsyncClient(transport=transport, base_url="http://agent.test") as client:
        response = await client.post(
            "/v2/lexicon/advance",
            json=body.model_dump(mode="json"),
            headers={"Authorization": "Bearer " + settings.api_token.get_secret_value()},
        )
    assert response.status_code == 200
    assert response.json()["proposals"]["grammar"] == [
        {"pos": "verb", "pattern": "run something + adv./prep."}
    ]


@pytest.mark.parametrize(
    ("location", "rules", "headword", "follows", "status"),
    [
        (
            "/dictionary/english/real-estate?q=real%2Bestate",
            "Allow: /",
            "real estate",
            True,
            "found",
        ),
        (
            "/dictionary/english/real-estate?q=real%2Bestate",
            "Disallow: /dictionary/english/real-estate",
            "real estate",
            False,
            "unavailable",
        ),
        (
            "https://example.test/dictionary/english/real-estate?q=real%2Bestate",
            "Allow: /",
            "real estate",
            False,
            "unavailable",
        ),
        ("/search/english/?q=real%2Bestate", "Allow: /", "real estate", False, "unavailable"),
        (
            "/dictionary/english/real-estate?q=real%2Bestate&other=1",
            "Allow: /",
            "real estate",
            False,
            "unavailable",
        ),
        (
            "/dictionary/english/real-estate-agent?q=real%2Bestate",
            "Allow: /",
            "real estate agent",
            True,
            "unavailable",
        ),
    ],
)
async def test_cambridge_redirect_preserves_exact_phrase_evidence(
    location, rules, headword, follows, status
):
    visited = []

    def reply(request):
        assert request.url.host == "dictionary.cambridge.org"
        visited.append(request.url.raw_path)
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=f"User-agent: *\n{rules}")
        if request.url.raw_path == b"/dictionary/english/real%20estate":
            return httpx.Response(302, headers={"Location": location})
        return httpx.Response(
            200,
            headers={"Content-Type": "text/html"},
            text=f'''<meta name="description" content="{headword} definition: Example.">
              <div class="entry-body__el"><span class="dhw">{headword}</span>
              <div class="dpos-h"><span class="dpos">noun</span>
              <span class="uk"><span class="ipa">ˈrɪəl ɪˌsteɪt</span></span>
              <span class="us"><span class="ipa">ˈriː.əl ɪˌsteɪt</span></span></div></div>''',
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as client:
        (material,) = await search_dictionaries(
            "real estate", None, client, source_names=("Cambridge Dictionary",)
        )

    expected_requests = [b"/robots.txt", b"/dictionary/english/real%20estate"]
    if follows:
        expected_requests.append(location.encode())
    assert visited == expected_requests
    assert material.status == status
    if status == "found":
        assert material.locator == "https://dictionary.cambridge.org" + location
        assert "英 [ˈrɪəl ɪˌsteɪt] / 美 [ˈriː.əl ɪˌsteɪt] · 词性 noun" in material.excerpt
    else:
        assert material.excerpt == ""


async def test_dictionary_reference_retries_only_the_selected_source(settings, monkeypatch):
    settings = settings.model_copy(
        update={"supervisor_enabled": True, "deepseek_api_key": SecretStr("local-test-only")}
    )
    name = "Cambridge Dictionary"
    calls = []

    async def fetch(headword, key, client, *, source_names, **options):
        calls.append((headword, source_names))
        return [
            Material(
                source=name,
                locator="https://dictionary.cambridge.org/dictionary/english/center",
                status="found" if len(calls) == 2 else "unavailable",
                excerpt="center · 词性 noun" if len(calls) == 2 else "",
            )
        ]

    monkeypatch.setattr(runtime, "search_dictionaries", fetch)
    auth = {"Authorization": "Bearer " + settings.api_token.get_secret_value()}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(create_app(settings)), base_url="http://agent.test"
    ) as client:
        capability = await client.get("/v2/lexicon/capability", headers=auth)
        assert capability.status_code == 200
        assert capability.json() == {
            "generation": True,
            "generation_candidates": True,
            "grammar_sub_pos": True,
            "section_generation_candidates": True,
            "dictionary_refresh": True,
        }
        path = "/v2/lexicon/dictionary-reference"
        request_body = {"headword": "center", "source": name}
        assert (await client.post(path, json=request_body)).status_code == 401
        unsupported = await client.post(
            path, headers=auth, json={**request_body, "source": "https://example.com"}
        )
        assert unsupported.status_code == 422
        first = await client.post(path, headers=auth, json=request_body)
        second = await client.post(path, headers=auth, json=request_body)
        assert first.status_code == second.status_code == 200
        assert first.json()["status"] == "unavailable"
        assert second.json()["status"] == "found"
    assert calls == [("center", (name,)), ("center", (name,))]


async def test_concurrent_replay_and_invalid_sense_do_not_duplicate_calls(
    store, settings, monkeypatch
):
    body = request(["definitions", "equivalents"])
    started, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def sources(*args, **kwargs):
        return [Material(source="有道", locator="", excerpt="", status="unavailable")]

    async def model(settings, scope, section, view):
        calls.append(section)
        if section == "senses":
            started.set()
            await release.wait()
            return {"items": [{"pos": "noun", "gloss": "明天", "level": "A1"}]}, {}
        if section == "definitions":
            return {"items": [{"sense": 3, "english": "x", "explanation": "x"}]}, {}
        return {"items": [{"sense": 0, "word": "明天"}]}, {}

    monkeypatch.setattr(runtime, "infer", model)
    monkeypatch.setattr(runtime, "search_dictionaries", sources)
    first = asyncio.create_task(runtime.advance(store, settings, body))
    await asyncio.wait_for(started.wait(), 4)
    overlap = await runtime.advance(store, settings, body)
    assert overlap.steps[0].state == "running" and calls == ["senses"]
    release.set()
    await first
    await runtime.advance(store, settings, body)
    done = await runtime.advance(store, settings, body)
    assert done.done and done.requires_human_review
    assert [s.state for s in done.steps] == ["completed", "failed", "completed"]
    assert done.proposals.equivalents[0].word == "明天" and not done.proposals.definitions
    assert await runtime.advance(store, settings, body) == done
    assert calls == ["senses", "definitions", "equivalents"]
    body.scope.actor_id = uuid4()
    with pytest.raises(Conflict):
        await runtime.advance(store, settings, body)


def new_run(body, **scope_changes):
    return body.model_copy(
        update={"scope": body.scope.model_copy(update={"run_id": uuid4(), **scope_changes})}
    )


@pytest.mark.parametrize("section", ["forms", "grammar", "groups"])
async def test_lookup_and_new_run_reuse_without_dictionary_http_or_model(
    store, settings, monkeypatch, section
):
    settings = settings.model_copy(
        update={
            "supervisor_enabled": True,
            "deepseek_api_key": SecretStr("local-test-only"),
            "youdao_public_lookup_enabled": True,
            "merriam_webster_api_key": SecretStr("local-test-only"),
        }
    )
    body = request([section])
    if section == "groups":
        body.scope.senses = [SenseContext(pos="noun", gloss="明天")]
    body.materials = [
        Material(source="智能词库", locator="entry@published-1", excerpt="明天", status="found")
    ]
    dictionary_requests = []
    model_calls = []

    def dictionary_reply(req):
        dictionary_requests.append(req.url.host)
        if req.url.path == "/robots.txt":
            return httpx.Response(
                200, text="User-agent: *\nAllow: /", headers={"content-type": "text/plain"}
            )
        if req.url.host == "www.dictionaryapi.com":
            return httpx.Response(200, json=[])
        return httpx.Response(404)

    async def dictionary_client(headword, key, client, **options):
        async with httpx.AsyncClient(transport=httpx.MockTransport(dictionary_reply)) as mock:
            return await search_dictionaries(headword, key, mock, **options)

    async def model(settings, instruction, context):
        model_calls.append(context["target"])
        if section == "grammar":
            items = [{"pos": "noun", "pattern": "by tomorrow"}]
        elif section == "groups":
            items = [{"sense": 0, "name_zh": "未来时间", "name_en": "future time"}]
        else:
            variant = {"spelling": "tomorrow", "dict_phonetic": "təˈmɒrəʊ"}
            items = [
                {
                    "pos": "noun",
                    "forms": [
                        {"form_type": "base", "is_regular": True, "uk": variant, "us": variant}
                    ],
                }
            ]
        return {"items": items}, {"model": "local-stub"}

    monkeypatch.setattr(runtime, "search_dictionaries", dictionary_client)
    monkeypatch.setattr(runtime.gateway, "complete", model)
    auth = {"Authorization": "Bearer " + settings.api_token.get_secret_value()}
    opted_in = {**auth, "x-tsz-next-section-candidates": "v1"} if section == "groups" else auth
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(create_app(settings, store)), base_url="http://agent.test"
    ) as client:

        async def send(operation, payload, headers=opted_in):
            response = await client.post(
                f"/v2/lexicon/{operation}", headers=headers, json=payload.model_dump(mode="json")
            )
            assert response.status_code == 200, response.text
            return response.json()

        assert (await client.post("/v2/lexicon/candidates", json={})).status_code == 401
        assert await send("candidates", body) == {"candidate": None}
        assert not dictionary_requests and not model_calls
        first = await send("advance", body)
        assert first["done"] and first["requires_human_review"] and first["proposals"][section]
        assert len(set(dictionary_requests)) == 4
        requests_after_first = list(dictionary_requests)

        # Snapshot and revision are rebound even after an unrelated manual save.
        second = new_run(body, base_revision=3, snapshot_id="b" * 64)
        read = (await send("candidates", second))["candidate"]
        assert read["source_run_id"] == first["run_id"] and read["generated_at"]
        assert read["result"]["run_id"] == str(second.scope.run_id)
        assert read["result"]["base_revision"] == 3
        assert read["result"]["snapshot_id"] == second.scope.snapshot_id
        assert read["result"]["sources"] == first["sources"]
        if section == "groups":
            assert await send("candidates", second, auth) == {"candidate": None}
        assert await send("advance", second) == read["result"]
        assert await send("advance", second) == read["result"]
        assert dictionary_requests == requests_after_first
        assert model_calls == ["en-GB"]

        for mismatched in (
            new_run(body, actor_id=uuid4()),
            new_run(body, entry_id=uuid4()),
            new_run(body, headword="yesterday"),
            new_run(body, target="en-US"),
            new_run(body, medium="zh-Hant"),
            new_run(body, senses=[SenseContext(pos="noun", gloss="隔天")]),
            new_run(body, existing_pos=["verb"]),
            new_run(body, catalog={"noun": ["base", "plural"]}),
            new_run(body, examples=["See you tomorrow."]),
            body.model_copy(
                update={
                    "materials": [
                        body.materials[0].model_copy(update={"locator": "entry@published-2"})
                    ]
                }
            ),
        ):
            assert await send("candidates", mismatched) == {"candidate": None}
        with monkeypatch.context() as rules:
            rules.setattr(candidates, "RULES_VERSION", "updated")
            assert await send("candidates", body) == {"candidate": None}
        refreshed = new_run(body)
        third = await send("refresh", refreshed)
        assert third["proposals"] == first["proposals"]
        assert await send("refresh", refreshed) == third
        assert dictionary_requests == requests_after_first
        assert model_calls == ["en-GB", "en-GB"]
        assert (await send("candidates", second))["candidate"]["source_run_id"] == third["run_id"]


def dictionary_result(name):
    source = runtime.DICTIONARIES[name]
    return Material(
        source=name,
        locator=source.url("tomorrow"),
        excerpt="tomorrow · 词性 noun",
        status="found",
    )


async def test_sense_directory_caches_dialect_with_senses_only_after_completion(
    store, settings, monkeypatch
):
    body = request(["senses", "dialect"])
    material = Material(
        source="智能词库",
        locator="entry@published-1",
        excerpt="British usage",
        status="found",
    )
    body.materials = [material]

    async def search(*args, **kwargs):
        return []

    async def model(settings, scope, section, view):
        if section == "senses":
            return {"items": [{"pos": "noun", "gloss": "次日", "level": "A1"}]}, {}
        return {
            "items": [
                {
                    "sense": 0,
                    "usage": "en-GB",
                    "source_locator": material.locator,
                    "evidence": "British usage",
                }
            ]
        }, {}

    monkeypatch.setattr(runtime, "infer", model)
    monkeypatch.setattr(runtime, "search_dictionaries", search)
    first = await runtime.advance(store, settings, body, mode="personal")
    assert not first.done
    assert (await runtime.lookup(store, settings, new_run(body), mode="personal")).candidate is None
    completed = await runtime.advance(store, settings, body, mode="personal")
    assert completed.done
    assert completed.proposals.dialect[0].usage == "en-GB"
    cached = (await runtime.lookup(store, settings, new_run(body), mode="personal")).candidate
    assert cached is not None
    assert cached.result.proposals == completed.proposals
    assert [step.section for step in cached.result.steps] == ["senses", "dialect"]


@pytest.mark.parametrize(
    ("sections", "rows"),
    [
        (
            ["forms"],
            [
                [
                    {
                        "pos": "noun",
                        "forms": [
                            {
                                "form_type": "base",
                                "is_regular": True,
                                "uk": {"spelling": "tomorrow", "dict_phonetic": "təˈmɒrəʊ"},
                                "us": {"spelling": "tomorrow", "dict_phonetic": "təˈmɑːroʊ"},
                            }
                        ],
                    }
                ]
            ],
        ),
        (["grammar"], [[{"pos": "noun", "pattern": "by tomorrow"}]]),
        (["groups"], [[{"sense": 0, "name_zh": "时间", "name_en": "Time"}]]),
        (
            ["senses", "dialect"],
            [
                [{"pos": "noun", "gloss": "未来", "level": "A1"}],
                [
                    {
                        "sense": 1,
                        "usage": "en-GB",
                        "source_locator": "entry@published-1",
                        "evidence": "British",
                    }
                ],
            ],
        ),
        (["definitions"], [[{"sense": 0, "english": "the next day", "explanation": "明天"}]]),
        (["equivalents"], [[{"sense": 0, "word": "明日"}]]),
        (
            ["relations"],
            [[{"sense": 0, "kind": "synonym", "word": "future", "gloss": "未来", "score": 90}]],
        ),
        (["examples"], [[{"sense": 0, "text": "See you tomorrow.", "level": "A1"}]]),
        (["translations"], [[{"example": 0, "text": "明天见。"}]]),
    ],
)
async def test_shared_candidates_reopen_by_entry_and_scope(
    store, settings, monkeypatch, sections, rows
):
    settings = settings.model_copy(
        update={"supervisor_enabled": True, "deepseek_api_key": SecretStr("local-test-only")}
    )
    body = request(sections)
    body.materials = [
        Material(source="智能词库", locator="entry@published-1", excerpt="British", status="found")
    ]
    if sections == ["translations"]:
        body.scope.examples = ["See you tomorrow."]
        body.scope.existing_pos = []
    else:
        body.scope.senses = [SenseContext(pos="noun", gloss="次日")]
    calls = []

    async def infer(settings, scope, section, view):
        calls.append(section)
        return {"items": rows[sections.index(section)]}, {"model": "local-stub"}

    async def fetch(settings, headword, names):
        return [dictionary_result(name) for name in names]

    monkeypatch.setattr(runtime, "infer", infer)
    monkeypatch.setattr(runtime, "fetch_dictionaries", fetch)
    auth = {"Authorization": "Bearer " + settings.api_token.get_secret_value()}
    sharing = {**auth, "x-tsz-next-shared-candidates": "v1"}
    colleague = new_run(body, actor_id=uuid4(), base_revision=3)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(create_app(settings, store)), base_url="http://agent.test"
    ) as client:

        async def send(operation, payload, headers=sharing):
            response = await client.post(
                f"/v2/lexicon/{operation}", headers=headers, json=payload.model_dump(mode="json")
            )
            assert response.status_code == 200, response.text
            return response.json()

        assert await send("candidates", colleague) == {"candidate": None}
        for _ in sections:
            first = await send("advance", body)
        assert first["done"] and all(first["proposals"][section] for section in sections)
        reopened = (await send("candidates", colleague))["candidate"]
        assert reopened["source_run_id"] == first["run_id"]
        assert reopened["result"]["proposals"] == first["proposals"]
        assert reopened["result"]["run_id"] == str(colleague.scope.run_id)
        assert reopened["result"]["base_revision"] == 3
        assert await send("advance", colleague) == reopened["result"]

        for mismatched in (
            new_run(colleague, entry_id=uuid4()),
            new_run(colleague, snapshot_id="b" * 64),
            new_run(colleague, target="en-US"),
        ):
            assert await send("candidates", mismatched) == {"candidate": None}
        assert await send("candidates", colleague, auth) == {"candidate": None}
        assert calls == sections


async def test_only_missing_dictionary_is_queried_and_new_evidence_invalidates_candidate(
    store, settings, monkeypatch
):
    body = request(["grammar"])
    calls = []
    models = []

    async def search(*args, source_names, **options):
        calls.append(source_names)
        materials = [dictionary_result(name) for name in source_names]
        if len(calls) == 1:
            materials[-1].status = "unavailable"
            materials[-1].excerpt = ""
        return materials

    async def model(settings, scope, section, view):
        models.append(scope.run_id)
        if len(models) == 2:
            raise ValueError("invalid_output")
        return {"items": [{"pos": "noun", "pattern": "by tomorrow"}]}, {}

    monkeypatch.setattr(runtime, "search_dictionaries", search)
    monkeypatch.setattr(runtime, "infer", model)

    first = await runtime.advance(store, settings, body)
    assert first.steps[0].state == "completed"
    assert (await runtime.lookup(store, settings, new_run(body))).candidate
    other = new_run(body, entry_id=uuid4(), actor_id=uuid4())
    assert (await runtime.advance(store, settings, other)).steps[0].state == "failed"
    assert calls == [tuple(runtime.DICTIONARIES), (next(reversed(runtime.DICTIONARIES)),)]
    assert (await runtime.lookup(store, settings, new_run(body))).candidate is None

    latest = new_run(body)
    result = await runtime.advance(store, settings, latest)
    assert result.done and all(source.status == "found" for source in result.sources)
    assert len(calls) == 2
    assert models == [body.scope.run_id, other.scope.run_id, latest.scope.run_id]


async def test_concurrent_dictionary_lookup_reuses_the_first_results(store, settings, monkeypatch):
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []
    first = request(["grammar"])
    second = new_run(first, entry_id=uuid4(), actor_id=uuid4())

    async def search(*args, source_names, **options):
        calls.append(source_names)
        entered.set()
        await release.wait()
        return [dictionary_result(name) for name in source_names]

    async def model(*args):
        return {"items": [{"pos": "noun", "pattern": "by tomorrow"}]}, {}

    monkeypatch.setattr(runtime, "search_dictionaries", search)
    monkeypatch.setattr(runtime, "infer", model)
    async with asyncio.TaskGroup() as tasks:
        one = tasks.create_task(runtime.advance(store, settings, first))
        await asyncio.wait_for(entered.wait(), 4)
        two = tasks.create_task(runtime.advance(store, settings, second))
        try:
            async with asyncio.timeout(4), store.sessions() as db:
                while not await db.scalar(
                    text(
                        "SELECT EXISTS (SELECT 1 FROM pg_locks WHERE locktype='advisory' AND NOT granted)"
                    )
                ):
                    await asyncio.sleep(0.01)
        finally:
            release.set()
    assert one.result().sources == two.result().sources
    assert calls == [tuple(runtime.DICTIONARIES)]


async def test_refresh_uses_legacy_evidence_for_the_same_scope(store, settings, monkeypatch):
    body = request(["grammar"])

    async def search(*args, source_names, **options):
        return [dictionary_result(name) for name in source_names]

    async def unexpected(*args, **kwargs):
        pytest.fail("references from a matching legacy candidate should be used")

    async def model(*args):
        return {"items": [{"pos": "noun", "pattern": "by tomorrow"}]}, {}

    monkeypatch.setattr(runtime, "search_dictionaries", search)
    monkeypatch.setattr(runtime, "infer", model)
    first = await runtime.advance(store, settings, body)

    # Restore the storage shape from before evidence snapshots were added.
    legacy = candidates.candidate_context(settings, body)
    assert legacy is not None
    async with store.sessions.begin() as db:
        await db.execute(
            update(candidates.StoredCandidate)
            .where(candidates.StoredCandidate.source_run_id == first.run_id)
            .values(fingerprint=legacy[0][3])
        )
        await db.execute(
            delete(dictionary_materials.StoredDictionaryMaterial).where(
                dictionary_materials.StoredDictionaryMaterial.query_key
                == dictionary_materials.query_key(settings, body.scope.headword)
            )
        )

    monkeypatch.setattr(runtime, "search_dictionaries", unexpected)
    current = await runtime.lookup(store, settings, new_run(body))
    assert current.candidate and current.candidate.source_run_id == first.run_id
    refreshed = await runtime.advance(store, settings, new_run(body), force_refresh=True)
    assert refreshed.done and refreshed.sources == first.sources
    next_lookup = await runtime.lookup(store, settings, new_run(body))
    assert next_lookup.candidate and next_lookup.candidate.source_run_id == refreshed.run_id


async def test_only_current_success_can_replace_grammar_candidate(store, settings, monkeypatch):
    body = request(["grammar"])
    started, release = asyncio.Event(), asyncio.Event()
    slow = new_run(body)
    cancelled = new_run(body)
    expired = new_run(body)
    first = new_run(body)
    fresh = new_run(body)
    empty = new_run(body)
    invalid = new_run(body)

    async def sources(*args, **kwargs):
        return []

    async def model(settings, scope, section, view):
        if scope.run_id == slow.scope.run_id:
            started.set()
            await release.wait()
        if scope.run_id == expired.scope.run_id:
            async with store.sessions.begin() as session:
                await session.execute(
                    update(SupervisorRun)
                    .where(SupervisorRun.id == scope.run_id)
                    .values(deadline=datetime.now(UTC) - timedelta(seconds=1))
                )
        if scope.run_id == empty.scope.run_id:
            return {"items": []}, {}
        pos = "verb" if scope.run_id == invalid.scope.run_id else "noun"
        return {"items": [{"pos": pos, "pattern": "until tomorrow"}]}, {}

    monkeypatch.setattr(runtime, "search_dictionaries", sources)
    monkeypatch.setattr(runtime, "infer", model)

    assert (await runtime.advance(store, settings, first)).done
    older = asyncio.create_task(runtime.advance(store, settings, slow, force_refresh=True))
    await asyncio.wait_for(started.wait(), 4)
    try:
        assert (await runtime.advance(store, settings, fresh, force_refresh=True)).done
    finally:
        release.set()
        assert (await older).done

    assert (await runtime.advance(store, settings, empty, force_refresh=True)).steps[
        0
    ].state == "completed"
    assert (await runtime.advance(store, settings, invalid, force_refresh=True)).steps[
        0
    ].state == "failed"
    assert (await runtime.advance(store, settings, expired, force_refresh=True)).done

    def disconnected():
        return True

    assert (
        await runtime.advance(
            store, settings, cancelled, force_refresh=True, disconnected=disconnected
        )
    ).done
    read = (await runtime.lookup(store, settings, new_run(body))).candidate
    assert read and read.source_run_id == fresh.scope.run_id


async def test_http_disconnect_fences_reuse_while_preserving_run_replay(
    store, settings, monkeypatch
):
    settings = settings.model_copy(
        update={"supervisor_enabled": True, "deepseek_api_key": SecretStr("local-test-only")}
    )
    body = request(["grammar"])
    started, release, disconnect = asyncio.Event(), asyncio.Event(), asyncio.Event()
    observed = asyncio.Event()

    async def sources(*args, **kwargs):
        return []

    async def model(settings, scope, section, view):
        started.set()
        await release.wait()
        return {"items": [{"pos": "noun", "pattern": "by tomorrow"}]}, {}

    monkeypatch.setattr(runtime, "search_dictionaries", sources)
    monkeypatch.setattr(runtime, "infer", model)
    wire_body = body.model_dump_json().encode()
    sent_body = False

    async def receive():
        nonlocal sent_body
        if not sent_body:
            sent_body = True
            return {"type": "http.request", "body": wire_body, "more_body": False}
        await disconnect.wait()
        observed.set()
        return {"type": "http.disconnect"}

    async def send(message):
        pass

    app = create_app(settings, store)
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.4"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/v2/lexicon/advance",
        "query_string": b"",
        "headers": [
            (b"authorization", b"Bearer " + settings.api_token.get_secret_value().encode()),
            (b"content-type", b"application/json"),
        ],
    }
    async with asyncio.TaskGroup() as tasks:
        tasks.create_task(app(scope, receive, send))
        async with asyncio.timeout(4):
            await started.wait()
            disconnect.set()
            await observed.wait()
            release.set()

    assert (await runtime.lookup(store, settings, new_run(body))).candidate is None
    replay = await runtime.advance(store, settings, body)
    assert replay.done and replay.proposals.grammar[0].pattern == "by tomorrow"
    assert (await runtime.lookup(store, settings, new_run(body))).candidate is None


def test_reuse_scope_preserves_multi_step_plans(settings):
    assert candidates.candidate_context(settings, request(["forms", "grammar"])) is None
    assert (
        candidates.candidate_context(settings, new_run(request(["grammar"]), existing_pos=[]))
        is None
    )


async def test_dictionary_source_status_from_actual_responses():
    def reply(req):
        assert not req.url.host.endswith(".youdao.com")
        if req.url.path == "/robots.txt":
            return httpx.Response(
                200,
                text="User-agent: *\nDisallow: /"
                if "oxford" in req.url.host
                else "User-agent: *\nAllow: /",
            )
        if req.url.host == "dictionary.cambridge.org":
            return httpx.Response(
                200,
                headers={"Content-Type": "text/html"},
                text='<meta name="description" content="Tomorrow: the next day.">',
            )
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as client:
        sources = await search_dictionaries("tomorrow", None, client)
    assert [s.status for s in sources] == ["not_configured", "unavailable", "no_match", "found"]
    assert sources[0].locator == "https://dict.youdao.com/w/eng/tomorrow/"
    assert sources[-1].excerpt == "Tomorrow: the next day."


async def test_dictionary_source_with_overlong_encoded_locator_stays_isolated():
    seen = []

    def reply(req):
        seen.append(req.url.host)
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as client:
        sources = await search_dictionaries("词" * 200, None, client, youdao_public_lookup=True)

    assert sources[0].status == "unavailable"
    assert sources[0].locator == ""
    assert "dict.youdao.com" not in seen
    assert len(sources) == 4


async def test_merriam_webster_collegiate_reads_exact_entry_and_redacts_api_key(caplog):
    key = "test-only-collegiate-key"
    requests = []

    def reply(req):
        requests.append(req.url)
        if req.url.host != "www.dictionaryapi.com":
            return httpx.Response(404)
        assert req.url.path == "/api/v3/references/collegiate/json/center"
        assert req.url.params["key"] == key
        return httpx.Response(
            200,
            json=[
                {
                    "meta": {
                        "id": "center:1",
                        "stems": [
                            "Centers",
                            "center",
                            "center of symmetry",
                            "centerless",
                            "centers",
                        ],
                    },
                    "hwi": {"hw": "cen*ter", "prs": [{"mw": "ˈsen-tər"}]},
                    "fl": "noun",
                },
                {
                    "meta": {
                        "id": "center:2",
                        "stems": ["center", "centered", "centering", "centers"],
                    },
                    "hwi": {"hw": "cen*ter", "prs": [{"mw": "ˈsen-tər"}]},
                    "fl": "verb",
                    "ins": [{"if": "cen*tered", "ifc": "-tered"}],
                    "def": [{"sseq": [[["sense", {"ins": [{"if": "cen*ter*ing"}]}]]]}],
                },
                {
                    "hwi": {"hw": "center on"},
                    "def": [
                        {
                            "sseq": [
                                [
                                    [
                                        "sense",
                                        {
                                            "dt": [
                                                ["vis", [{"t": "We {it}center on{/it} one issue."}]]
                                            ]
                                        },
                                    ]
                                ]
                            ]
                        }
                    ],
                },
                {"hwi": {"hw": "cen*ter"}, "fl": "verb", "ins": [{"if": "cen*tered"}]},
                {"hwi": {"hw": "cen*tral"}, "fl": "adjective"},
            ],
        )

    caplog.set_level("INFO", logger="httpx")
    async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as client:
        sources = await search_dictionaries(
            "center", None, client, merriam_webster_api_key=SecretStr(key)
        )
    source = next(row for row in sources if row.source == "Merriam-Webster")
    assert source.status == "found"
    assert source.locator == "https://www.merriam-webster.com/dictionary/center"
    assert source.excerpt == (
        "center · 词性 noun、verb · 读音标记 ˈsen-tər"
        " · 词形 noun [] · 词形 verb [centered、centering]"
        " · 索引词形 noun [复数 centers] · 索引词形 verb [第三人称单数 centers]"
        " · 例句证据 We center on one issue."
    )
    assert {url.host for url in requests} == {
        "www.dictionaryapi.com",
        "www.oxfordlearnersdictionaries.com",
        "dictionary.cambridge.org",
    }
    assert "www.dictionaryapi.com" in caplog.text
    assert key not in caplog.text


@pytest.mark.parametrize(
    ("label", "target", "expected"),
    [
        ("chiefly British spelling of", "labor", "found"),
        ("see also", "labor", "unavailable"),
        ("chiefly British spelling of", "https://example.com/labor", "unavailable"),
    ],
)
async def test_merriam_webster_collegiate_follows_exact_spelling_reference(
    label, target, expected, caplog
):
    requested = []

    def reply(req):
        if req.url.host != "www.dictionaryapi.com":
            return httpx.Response(404)
        word = req.url.path.rsplit("/", 1)[-1]
        requested.append(word)
        if word == "labour":
            return httpx.Response(
                200,
                json=[
                    {
                        "hwi": {"hw": "la*bour"},
                        "cxs": [{"cxl": label, "cxtis": [{"cxt": target}]}],
                    },
                    {"hwi": {"hw": "la*bor"}, "fl": "noun"},
                ],
            )
        assert word == "labor"
        return httpx.Response(
            200,
            json=[
                {
                    "hwi": {"hw": "la*bor", "prs": [{"mw": "ˈlā-bər"}]},
                    "fl": "noun",
                    "ins": [{"il": "plural", "if": "la*bors"}],
                },
                {
                    "hwi": {"hw": "labor"},
                    "fl": "verb",
                    "ins": [{"if": "la*bored"}, {"if": "la*bor*ing"}, {"if": "la*bors"}],
                    "def": [
                        {
                            "sseq": [
                                [["sense", {"dt": [["vis", [{"t": "They labor to repair it."}]]]}]]
                            ]
                        }
                    ],
                },
                {"hwi": {"hw": "laborer"}, "fl": "noun", "ins": [{"if": "laborers"}]},
            ],
        )

    key = "test-only-spelling-reference-key"
    caplog.set_level("INFO", logger="httpx")
    async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as client:
        sources = await search_dictionaries(
            "labour", None, client, merriam_webster_api_key=SecretStr(key)
        )

    source = next(row for row in sources if row.source == "Merriam-Webster")
    assert source.status == expected
    assert key not in caplog.text
    if expected == "found":
        assert requested == ["labour", "labor"]
        assert source.locator == "https://www.merriam-webster.com/dictionary/labor"
        assert source.excerpt == (
            "labour · 词性 noun、verb · 读音标记 ˈlā-bər"
            " · 词形 noun [labors] · 词形 verb [labored、laboring、labors]"
            " · [ 英式拼写 labour、美式拼写 labor ]"
            " · 例句证据 They labor to repair it."
        )
    else:
        assert requested == ["labour"]
        assert source.excerpt == ""


@pytest.mark.parametrize(
    ("word", "entries", "expected"),
    [
        (
            "test",
            [
                {"hwi": {"hw": "test"}, "fl": "adjective"},
                {"hwi": {"hw": "Test"}, "fl": "abbreviation"},
            ],
            "test · 词性 adjective、abbreviation · 词形 adjective [原形 test]"
            " · 词形 abbreviation [原形 Test]",
        ),
        (
            "good",
            [
                {"hwi": {"hw": "good"}, "fl": "adjective"},
                {
                    "hwi": {"hw": "good"},
                    "fl": "adjective",
                    "ins": [{"if": "bet*ter"}, {"if": "best"}],
                },
            ],
            "good · 词性 adjective · 词形 adjective [better、best]",
        ),
    ],
)
async def test_webster_preserves_original_headword_for_adjective_and_abbreviation(
    word, entries, expected
):
    def reply(req):
        if req.url.host != "www.dictionaryapi.com":
            return httpx.Response(404)
        assert req.url.path == f"/api/v3/references/collegiate/json/{word}"
        return httpx.Response(200, json=entries)

    async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as client:
        sources = await search_dictionaries(
            word, None, client, merriam_webster_api_key=SecretStr("test-only")
        )

    source = next(row for row in sources if row.source == "Merriam-Webster")
    assert source.status == "found"
    assert source.excerpt == expected


@pytest.mark.parametrize(
    ("status", "payload", "expected"),
    [
        (200, ["center", "centre"], "no_match"),
        (200, [], "no_match"),
        (200, {"message": "Invalid API key"}, "unavailable"),
        (200, [{"hwi": {"hw": "cen*ter"}}], "unavailable"),
        (302, None, "unavailable"),
        (403, None, "unavailable"),
        (429, None, "unavailable"),
    ],
)
async def test_merriam_webster_collegiate_distinguishes_misses_and_errors(
    status, payload, expected
):
    official_requests = []

    def reply(req):
        assert req.url.host not in {"www.merriam-webster.com", "redirect.test"}
        if req.url.host != "www.dictionaryapi.com":
            return httpx.Response(404)
        official_requests.append(req)
        return httpx.Response(
            status,
            json=payload,
            headers={"Location": "https://redirect.test/"} if status == 302 else None,
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(reply), follow_redirects=True
    ) as client:
        sources = await search_dictionaries(
            "center", None, client, merriam_webster_api_key=SecretStr("test-key")
        )
    source = next(row for row in sources if row.source == "Merriam-Webster")
    assert source.status == expected and source.excerpt == ""
    assert len(official_requests) == 1


@pytest.mark.parametrize(
    ("evidence", "expected"),
    [("chiefly British", "completed"), ("missing quote", "failed"), ("calendar day", "failed")],
)
async def test_dialect_agent_requires_fetched_regional_evidence(
    store, settings, monkeypatch, evidence, expected
):
    body = request(["dialect"])
    body.scope.senses.append(SenseContext(pos="noun", gloss="词义"))
    body.materials = [
        Material(
            source="智能词库",
            locator="entry@2",
            excerpt="chiefly British: calendar day",
            status="found",
        )
    ]

    async def lookup(*_, **__):
        return []

    async def model(*_):
        return {
            "items": [
                {"sense": 0, "usage": "en-GB", "source_locator": "entry@2", "evidence": evidence}
            ]
        }, {}

    monkeypatch.setattr(runtime, "search_dictionaries", lookup)
    monkeypatch.setattr(runtime, "infer", model)
    result = await runtime.advance(store, settings, body)
    assert result.done and result.steps[0].state == expected
    assert bool(result.proposals.dialect) == (expected == "completed")


async def test_search_provider_falls_back_after_live_lookup_and_filters_domains():
    indexed_hosts = []

    def reply(req):
        assert not req.url.host.endswith(".youdao.com")
        if req.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /")
        if req.url.host == "dictionary.cambridge.org":
            return httpx.Response(
                200,
                headers={"Content-Type": "text/html"},
                text='<meta name="description" content="Tomorrow from the live dictionary.">',
            )
        if req.url.host != "api.search.brave.com":
            return httpx.Response(403)

        assert req.headers["X-Subscription-Token"] == "local-test"
        host = req.url.params["q"].split(" ")[0].removeprefix("site:")
        indexed_hosts.append(host)
        return httpx.Response(
            200,
            json={
                "web": {
                    "results": [
                        {"url": "https://example.org/tomorrow", "description": "tomorrow"},
                        {
                            "url": f"https://{host}/tomorrow",
                            "title": "tomorrow",
                            "description": "The <b>next</b> day.",
                        },
                    ]
                }
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as client:
        materials = await search_dictionaries("tomorrow", SecretStr("local-test"), client)
    assert len(indexed_hosts) == 3
    assert "dict.youdao.com" in indexed_hosts
    assert "dictionary.cambridge.org" not in indexed_hosts
    assert materials[-1].excerpt == "Tomorrow from the live dictionary."
    assert all(
        m.excerpt == "The next day." and m.status == "found" and "example.org" not in m.locator
        for m in materials[:-1]
    )


@pytest.mark.parametrize(
    ("headword", "rules", "expected"),
    [
        ("test", "Allow: /", "found"),
        ("unrelated", "Allow: /", "unavailable"),
        ("center", "Disallow: /w/", "unavailable"),
    ],
)
async def test_youdao_uses_public_dictionary_rules_and_basic_entry(headword, rules, expected):
    entry_requests = []

    def respond(req):
        if req.url.host != "dict.youdao.com":
            return httpx.Response(404)
        if req.url.path == "/robots.txt":
            return httpx.Response(200, text=f"User-agent: *\n{rules}")
        entry_requests.append(req.url.path)
        return httpx.Response(
            200,
            headers={"Content-Type": "text/html"},
            text="""<meta name="description" content="General company description">
                <div id="phrsListTab">
                  <h2><span class="keyword">test</span>
                    <div><span class="pronounce">美<span>[test]</span></span>
                    <span class="pronounce">英 <span>[test]</span></span></div>
                  </h2>
                  <ul><li>adj. Unrelated list outside the definitions</li></ul>
                  <div class="trans-container"><ul><li>n. Sample text</li><li>v. Sample text</li></ul>
                  <p class="additional">[ 复数 tests 第三人称单数 tests 现在分词 testing
                    过去式 tested 过去分词 tested ]</p></div>
                </div>
                <span class="pronounce">美 [wrong]</span>""",
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        materials = await search_dictionaries(headword, None, client, youdao_public_lookup=True)

    source = next(row for row in materials if row.source == "有道")
    assert source.status == expected
    assert source.locator == f"https://dict.youdao.com/w/eng/{headword}/"
    assert entry_requests == ([] if rules.startswith("Disallow") else [f"/w/eng/{headword}/"])
    if expected == "found":
        assert (
            source.excerpt == "test · 英 [test] / 美 [test] · 词性 n.、v."
            " · [复数 tests 第三人称单数 tests 现在分词 testing 过去式 tested 过去分词 tested]"
            " · 释义 n. Sample text · 释义 v. Sample text"
        )
    else:
        assert source.excerpt == ""


async def test_youdao_returns_phrase_meaning_and_bilingual_examples_without_word_forms():
    def respond(req):
        if req.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /")
        assert req.url.raw_path == b"/w/eng/warm%20welcome/"
        return httpx.Response(
            200,
            headers={"Content-Type": "text/html"},
            text="""
                <div id="phrsListTab">
                  <h2><span class="keyword">warm welcome</span>
                    <span class="pronounce"><a title="发音"></a></span></h2>
                  <div class="trans-container"><ul><li>热烈欢迎</li></ul></div>
                </div>
                <div id="bilingual"><ul class="ol"><li>
                  <p><span>They gave us a </span><b>warm welcome</b><span> — a pleasant surprise.</span></p>
                  <p><span>他们</span><span>热情地欢迎了我们。</span></p>
                  <p class="example-via">来源名称</p>
                </li><li><p>They waved.</p></li></ul></div>
                <div id="authority"><p>Outside the bilingual examples.</p></div>
            """,
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        (source,) = await search_dictionaries(
            "warm welcome", None, client, youdao_public_lookup=True, source_names=("有道",)
        )

    assert source.status == "found"
    assert source.excerpt == (
        "warm welcome · 释义 热烈欢迎 · 例句原文 They gave us a warm welcome — a pleasant surprise."
        " · 例句译文 他们热情地欢迎了我们。 · 例句原文 They waved."
    )


@pytest.mark.parametrize(
    ("collins_head", "basic_forms", "expected_forms"),
    [
        ("school", "", " · [ schooling、schooled、schools ]"),
        ("unrelated", "", ""),
        ("school", '<p class="additional">[ 复数 schools ]</p>', " · [复数 schools]"),
    ],
)
def test_youdao_uses_matching_collins_forms_if_basic_entry_has_none(
    collins_head, basic_forms, expected_forms
):
    reader = YoudaoSummary()
    reader.feed(f"""
        <div id="phrsListTab">
            <h2><span class="keyword">school</span></h2>
            <div class="trans-container">
                <ul><li>n. School</li><li>v. Train</li></ul>
                {basic_forms}
            </div>
        </div>
        <div id="unrelated"><h4><span class="title">school</span>
            <span class="additional pattern">( wrong )</span></h4></div>
        <div id="collinsResult"><div class="wt-container">
            <h4><span class="title">{collins_head}</span>
                <span class="additional pattern">( schooling, schooled, schools )</span>
            </h4>
            <div class="examples"><p>They school them in safety.</p><p>中文译文。</p></div>
        </div></div>
    """)
    expected_example = (
        " · 例句证据 （柯林斯）They school them in safety." if collins_head == "school" else ""
    )
    assert (
        reader.excerpt("school", "")
        == "school · 词性 n.、v."
        + expected_forms
        + expected_example
        + " · 释义 n. School · 释义 v. Train"
    )
    assert reader.excerpt("schoolyard", "") == ""


@pytest.mark.parametrize(
    ("robots", "responses", "status", "entry_requests"),
    [
        ([503, 200], [502, 200], "found", 2),
        ([200], ["connect-error", 200], "found", 2),
        ([200], [503, 503], "unavailable", 2),
        ([200], ["retry-after"], "unavailable", 1),
        ([200], [429], "unavailable", 1),
        ([200], [403], "unavailable", 1),
        ([200], [404], "no_match", 1),
        ([200], [302], "unavailable", 1),
        ([302], [], "unavailable", 0),
        (["html"], [], "unavailable", 0),
    ],
)
async def test_youdao_retries_transient_errors_without_overriding_site_controls(
    robots, responses, status, entry_requests
):
    requested = []
    rules, entries = iter(robots), iter(responses)

    def respond(req):
        assert req.url.host != "redirect.test"
        if req.url.host != "dict.youdao.com":
            return httpx.Response(404)
        requested.append(req.url.path)
        if req.url.path == "/robots.txt":
            code = next(rules)
            if code == "html":
                return httpx.Response(200, headers={"Content-Type": "text/html"}, text="<html/>")
            if code == 200:
                return httpx.Response(200, text="User-agent: *\nAllow: /")
        else:
            assert req.url.raw_path == b"/w/eng/take%20off/"
            code = next(entries)
            if code == "connect-error":
                raise httpx.ConnectError("connection failed", request=req)
            if code == "retry-after":
                return httpx.Response(503, headers={"Retry-After": "30"})
            if code == 200:
                return httpx.Response(
                    200,
                    headers={"Content-Type": "text/html"},
                    text='<div id="phrsListTab"><span class="keyword">take off</span>'
                    '<span class="pronounce">英 [teɪk ɒf]</span></div>',
                )
        return httpx.Response(
            code, headers={"Location": "https://redirect.test/"} if code == 302 else None
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(respond), follow_redirects=True
    ) as client:
        sources = await search_dictionaries("take off", None, client, youdao_public_lookup=True)

    source = sources[0]
    assert source.status == status
    assert source.excerpt == ("take off · 英 [teɪk ɒf]" if status == "found" else "")
    assert requested == ["/robots.txt"] * len(robots) + ["/w/eng/take off/"] * entry_requests


@pytest.mark.parametrize(
    ("entry_redirect", "rules", "expected"),
    [
        ("/definition/english/center_1", "Allow: /", "found"),
        ("/definition/english/center_1", "Disallow: /definition/english/center_1", "unavailable"),
        ("https://example.org/definition/english/center_1", "Allow: /", "unavailable"),
        ("/search/english/?q=center", "Allow: /", "unavailable"),
    ],
)
async def test_oxford_follows_only_allowed_dictionary_redirects(entry_redirect, rules, expected):
    entry_requests = []
    oxford_host = "www.oxfordlearnersdictionaries.com"

    def respond(req):
        if req.url.host != oxford_host:
            assert req.url.path == "/robots.txt"
            return httpx.Response(503)
        if req.url.path == "/robots.txt":
            return httpx.Response(200, text=f"User-agent: *\n{rules}")
        entry_requests.append(req.url.path)
        if len(entry_requests) == 1:
            assert req.url.path == "/definition/english/center"
            return httpx.Response(302, headers={"Location": entry_redirect})
        assert req.url.path == "/definition/english/center_1"
        return httpx.Response(
            200,
            headers={"Content-Type": "text/html"},
            text='<meta name="description" content="center noun: the middle point">',
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        materials = await search_dictionaries("center", None, client)
    source = next(row for row in materials if row.source.startswith("Oxford"))
    assert source.status == expected
    if expected == "found":
        assert source.locator == f"https://{oxford_host}/definition/english/center_1"
        assert source.excerpt == "center noun: the middle point"
        assert len(entry_requests) == 2
    else:
        assert len(entry_requests) == 1
        assert source.excerpt == ""


@pytest.mark.parametrize(
    ("word", "pos", "annotation", "examples", "expected"),
    [
        (
            "child",
            "noun",
            '(plural <span class="inflected_form">children</span>'
            '<span class="phonetics"><div class="phons_br"><span class="phon">/ˈtʃɪldrən/</span>'
            "</div></span>)",
            [],
            "词形 noun [复数 children]",
        ),
        (
            "study",
            "noun",
            '(plural <span class="inflected_form">studies</span>)',
            ["Several studies support the conclusion."],
            "词形 noun [复数 studies]",
        ),
        (
            "formula",
            "noun",
            '(plural <span class="inflected_form">formulas</span> or '
            '<span class="inflected_form">formulae</span>)',
            [],
            "词形 noun [复数 formulas、复数 formulae]",
        ),
        (
            "happy",
            "adjective",
            '(comparative <span class="inflected_form">happier</span>, '
            'superlative <span class="inflected_form">happiest</span>)',
            [],
            "词形 adjective [比较级 happier、最高级 happiest]",
        ),
        (
            "book",
            "noun",
            "",
            ["His desk was covered with piles of <b>books</b>."],
            "词形 noun [复数 books]",
        ),
        (
            "nice",
            "adjective",
            "",
            ["It is nicer than before.", "I chose the nicest one."],
            "词形 adjective [比较级 nicer、最高级 nicest]",
        ),
        ("flea", "noun", "", ["The dog has fleas."], "词形 noun [复数 fleas]"),
        ("flea", "noun", "", ["The dog has fleas-related problems."], ""),
        (
            "venue",
            "noun",
            "",
            ["The band will be playing at <b>20 different</b> venues on their UK tour."],
            "词形 noun [复数 venues]",
        ),
        ("venue", "noun", "", ["She wrote 20 different venues-related notes."], ""),
        (
            "division",
            "noun",
            "",
            ["There are deep divisions in the party over the war."],
            "词形 noun [复数 divisions]",
        ),
        ("book", "noun", "", ["He edits some books-related articles."], ""),
    ],
)
def test_oxford_reads_headword_inflections_and_confirms_examples(
    word, pos, annotation, examples, expected
):
    entry = OxfordEntry(word)
    example_rows = "".join(f'<li><span class="x">{example}</span></li>' for example in examples)
    entry.feed(f"""
        <div id="entryContent">
            <div class="webtop">
                <h1 class="headword">{word}</h1><span class="pos">{pos}</span>
                <span class="phons_br"><span class="phon">/head/</span></span>
                <div class="inflections">{annotation}</div>
            </div>
            <span class="x">Some books outside the examples list.</span>
            <ul class="examples">{example_rows}</ul>
        </div>
    """)
    excerpt = f"{word} · 英 [head] · 词性 {pos}"
    if expected:
        excerpt += " · " + expected
    assert entry.excerpt(word, "") == excerpt


@pytest.mark.parametrize("note_at_headword", [True, False])
def test_oxford_reads_alternative_degrees_from_headword_note(note_at_headword):
    entry = OxfordEntry("guilty")
    note = (
        '<span class="un" un="help"><span class="eb">more guilty</span> and '
        '<span class="eb">most guilty</span> are more common</span>'
    )
    entry.feed(f"""
        <div id="entryContent">
            <div class="webtop">
                <h1 class="headword">guilty</h1><span class="pos">adjective</span>
                <div class="inflections">
                    (comparative <span class="inflected_form">guiltier</span>,
                    superlative <span class="inflected_form">guiltiest</span>)
                </div>
                {note if note_at_headword else ""}
            </div>
            {"" if note_at_headword else note}
        </div>
    """)

    expected = "guilty · 词性 adjective · 词形 adjective [比较级 guiltier、最高级 guiltiest"
    if note_at_headword:
        expected += "、比较级 more guilty、最高级 most guilty"
    assert entry.excerpt("guilty", "") == expected + "]"


@pytest.mark.parametrize(
    ("first", "second"),
    [("noun", "verb"), ("verb", "noun"), ("verb", "adjective")],
)
async def test_oxford_merges_only_verified_opposite_part_of_speech(first, second):
    urls = {"noun": "/definition/english/run_2", "verb": "/definition/english/run_1"}
    pages = {}
    requested = []
    for pos, other in (("noun", "verb"), ("verb", "noun")):
        body_pos = second if pos != first else first
        pages[urls[pos]] = f"""
            <div id="entryContent">
                <div class="webtop">
                    <h1 class="headword">run</h1><span class="pos">{body_pos}</span>
                </div>
                {
            ('<ul class="examples"><li><span class="x">Our team won by four runs.</span></li></ul>')
            if pos == "noun"
            else ""
        }
                {
            ('<table><tr><td class="verb_form">past simple ran</td></tr></table>')
            if pos == "verb"
            else ""
        }
            </div>
            <a href="https://example.org{urls[other]}" title="run {other} definition">wrong host</a>
            <a href="{urls[other]}" title="run {other} definition">related entry</a>
        """

    def reply(req):
        assert req.url.host != "example.org"
        if req.url.host != "www.oxfordlearnersdictionaries.com":
            return httpx.Response(404)
        if req.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /")
        requested.append(req.url.path)
        if req.url.path == "/definition/english/run":
            return httpx.Response(302, headers={"Location": urls[first]})
        return httpx.Response(200, headers={"Content-Type": "text/html"}, text=pages[req.url.path])

    async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as client:
        sources = await search_dictionaries("run", None, client)
    source = next(row for row in sources if row.source.startswith("Oxford"))
    opposite = "verb" if first == "noun" else "noun"
    assert requested == ["/definition/english/run", urls[first], urls[opposite]]
    assert source.locator.endswith(urls[first])
    assert source.status == "found"
    assert ("词形 noun [复数 runs]" in source.excerpt) == (second != "adjective")
    assert "词形 verb [过去式 ran]" in source.excerpt
    assert "adjective" not in source.excerpt


async def test_oxford_follows_noun_link_from_adverb_entry():
    host = "www.oxfordlearnersdictionaries.com"
    root = "/definition/english/"
    pages = {
        "maximum_1": """
            <div id="entryContent"><div class="webtop">
                <h1 class="headword">maximum</h1><span class="pos">adverb</span>
            </div></div>
            <a href="/definition/english/maximum_2" title="maximum adjective definition"></a>
            <a href="/definition/english/maximum_3" title="maximum noun definition"></a>
        """,
        "maximum_3": """
            <div id="entryContent"><div class="webtop">
                <h1 class="headword">maximum</h1><span class="pos">noun</span>
                <div class="inflections">(plural <span class="inflected_form">maxima</span>)</div>
            </div></div>
        """,
    }
    requested = []

    def respond(req):
        if req.url.host != host:
            return httpx.Response(404)
        if req.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /")
        slug = req.url.path.removeprefix(root)
        requested.append(slug)
        if slug == "maximum":
            return httpx.Response(302, headers={"Location": root + "maximum_1"})
        return httpx.Response(200, headers={"Content-Type": "text/html"}, text=pages[slug])

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        source = next(
            row
            for row in await search_dictionaries("maximum", None, client)
            if row.source.startswith("Oxford")
        )
    assert requested == ["maximum", "maximum_1", "maximum_3"]
    assert source.excerpt == "maximum · 词性 adverb、noun · 词形 noun [复数 maxima]"


@pytest.mark.parametrize(
    ("status", "html", "expected"),
    [
        (
            200,
            '<meta name="description" content="Browse the dictionary">'
            '<meta property="og:description" content="center: the middle point">',
            "found",
        ),
        (
            200,
            '<meta name="description" content="center: the middle point">'
            '<meta property="og:description" content="Browse the dictionary">',
            "found",
        ),
        (
            200,
            '<meta name="description" content="center: the middle point">' + " " * 410_000,
            "found",
        ),
        (200, '<meta name="description" content="Browse the dictionary">', "unavailable"),
        (
            403,
            '<meta name="description" content="center: the middle point">',
            "unavailable",
        ),
    ],
    ids=["generic-first", "entry-first", "large-chunk", "generic-only", "blocked"],
)
async def test_dictionary_returns_entry_metadata_only_from_successful_pages(status, html, expected):
    def respond(req):
        if req.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /")
        if req.url.host == "www.merriam-webster.com":
            return httpx.Response(status, headers={"Content-Type": "text/html"}, text=html)
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        sources = await search_dictionaries("center", None, client)
    source = next(row for row in sources if row.source == "Merriam-Webster")
    assert source.status == expected
    assert source.excerpt == ("center: the middle point" if expected == "found" else "")


async def test_dictionary_reads_main_entry_facts_and_keeps_provider_summary():
    pages = {
        "www.oxfordlearnersdictionaries.com": """
            <meta name="description" content="Definition of center noun in Oxford Advanced Learner's Dictionary.">
            <div id="entryContent"><div class="webtop">
              <h1 class="headword">center</h1><span class="pos">noun</span>
              <div class="phons_br"><span class="phon">/ˈsentə(r)/</span></div>
              <div class="phons_n_am"><span class="phon">/ˈsentər/</span></div>
            </div><span class="def">the middle point or part of something</span></div>
        """,
        "dictionary.cambridge.org": """
            <meta name="description" content="CENTER definition: 1. the middle point or part">
            <div class="entry-body__el"><div class="dpos-h">
              <span class="hw dhw">center</span><span class="pos dpos">noun</span>
              <span class="uk dpron-i"><span class="ipa">ˈsen.tə<span class="sp">r</span></span></span>
            </div></div>
        """,
        "www.merriam-webster.com": """
            <meta name="description" content="The meaning of CENTER is the middle point.">
        """
        + (" " * 410_000)
        + """<div id="dictionary-entry-1"><h1 class="hword">center</h1>
              <h2 class="parts-of-speech">noun</h2><a class="prons-entry-list-item">ˈsen-tər</a>
            </div>
            <div id="dictionary-entry-2"><p class="hword">center</p>
              <h2 class="parts-of-speech">verb</h2>
              <div class="vg-ins"><span class="if">centered</span>; <span class="if">centering</span></div>
            </div>
            <div id="dictionary-entry-3"><p class="hword">central</p>
              <h2 class="parts-of-speech">adjective</h2></div>
        """,
    }

    def respond(req):
        if req.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /")
        if req.url.host in pages:
            return httpx.Response(
                200, headers={"Content-Type": "text/html"}, text=pages[req.url.host]
            )
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        entries = {row.source: row for row in await search_dictionaries("center", None, client)}
    oxford = entries["Oxford Learner’s Dictionaries"]
    assert oxford.status == "found"
    assert "英 [ˈsentə(r)] / 美 [ˈsentər] · 词性 noun" in oxford.excerpt
    assert oxford.excerpt.endswith("释义 the middle point or part of something")
    cambridge = entries["Cambridge Dictionary"]
    assert "英 [ˈsen.tər] · 词性 noun · 来源摘要 CENTER definition" in cambridge.excerpt
    webster = entries["Merriam-Webster"]
    assert "词性 noun、verb · 读音标记 ˈsen-tər" in webster.excerpt
    assert "词形 noun [] · 词形 verb [centered、centering]" in webster.excerpt
    assert "adjective" not in webster.excerpt


@pytest.mark.parametrize("form_lemma", ["centre", "centralize"])
async def test_live_entries_follow_only_verified_related_lexemes(form_lemma):
    requested = set()

    def respond(req):
        if req.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /")
        requested.add((req.url.host, req.url.path))
        html = {
            (oxford_host, "/definition/english/center"): """
                <div id="entryContent"><div class="webtop">
                    <h1 class="headword">center</h1><span class="pos">noun</span>
                </div></div>
                <a href="https://example.org/definition/english/center_2" title="center verb definition">wrong</a>
                <a href="/definition/english/center_2" title="center verb definition">center verb</a>
            """,
            (oxford_host, "/definition/english/center_2"): """
                <div id="entryContent"><div class="webtop">
                    <h1 class="headword">center</h1><span class="pos">verb</span>
                </div>
                    <table><tr><td class="verb_form">
                        <span class="vf_prefix">past simple</span> centered
                    </td></tr><tr><td class="verb_form">
                        <span class="vf_prefix">-ing form</span> centering
                    </td></tr></table>
                </div>
            """,
            (cambridge_host, "/dictionary/english/center"): """
                <meta name="description" content="CENTER definition: 1. US spelling of centre 2. the middle point">
                <div class="entry-body__el"><div class="dpos-h">
                    <span class="dhw">center</span><span class="dpos">noun</span>
                    <span class="uk"><span class="ipa">ˈsen.tə<span class="dsp">r</span></span></span>
                </div></div>
            """,
            (cambridge_host, "/dictionary/english/centre"): """
                <div class="entry-body__el"><div class="dpos-h">
                    <span class="dhw">centre</span><span class="dpos">verb</span>
                </div><span class="dinf">centred</span></div>
            """,
            (cambridge_host, "/dictionary/english/centring"): f"""
                <meta name="description" content="CENTRING definition: 1. present participle of {form_lemma} 2. a description">
            """,
        }.get((req.url.host, req.url.path))
        if html is None:
            return httpx.Response(403)
        return httpx.Response(200, headers={"Content-Type": "text/html"}, text=html)

    oxford_host = "www.oxfordlearnersdictionaries.com"
    cambridge_host = "dictionary.cambridge.org"
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        entries = {row.source: row for row in await search_dictionaries("center", None, client)}

    oxford = entries["Oxford Learner’s Dictionaries"]
    assert "词性 noun、verb" in oxford.excerpt
    assert "词形 verb [过去式 centered、现在分词 centering]" in oxford.excerpt
    cambridge = entries["Cambridge Dictionary"]
    assert "词性 noun、verb" in cambridge.excerpt
    assert "英 [ˈsen.tə(r)]" in cambridge.excerpt
    assert "英式拼写 centre、美式拼写 center" in cambridge.excerpt
    assert "词形 verb [centred" in cambridge.excerpt
    assert ("现在分词 centring" in cambridge.excerpt) == (form_lemma == "centre")
    assert (cambridge_host, "/dictionary/english/centring") in requested
    assert entries["Merriam-Webster"].status == "unavailable"
    assert not any(host == "example.org" for host, _ in requested)


@pytest.mark.parametrize(
    ("location", "found"),
    [
        ("rely-on-upon", True),
        ("https://example.org/dictionary/english/rely-on-upon", False),
        ("/translate/english/rely-on-upon", False),
    ],
)
async def test_cambridge_follows_only_dictionary_entry_redirects(location, found):
    host = "dictionary.cambridge.org"
    entry = "/dictionary/english/rely"
    canonical_entry = "/dictionary/english/rely-on-upon"
    requests = []

    def respond(req):
        requests.append((req.url.host, req.url.path))
        if req.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /")
        if req.url.path == entry:
            return httpx.Response(301, headers={"Location": location})
        if req.url.path == canonical_entry:
            return httpx.Response(
                200,
                headers={"Content-Type": "text/html"},
                text="""
                    <div class="entry-body__el"><div class="dpos-h">
                        <span class="dhw">rely</span><span class="dpos">verb</span>
                        <span class="dgram">[ <span class="dgc">+ to infinitive</span> ]</span>
                    </div></div>
                """,
            )
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        (source,) = await search_dictionaries(
            "rely", None, client, source_names=("Cambridge Dictionary",)
        )

    if found:
        assert source.status == "found"
        assert source.locator == f"https://{host}{canonical_entry}"
        assert source.excerpt == "rely · 词性 verb · 句型 + to infinitive"
        assert (host, canonical_entry) in requests
    else:
        assert source.status == "unavailable"
        assert requests == [(host, "/robots.txt"), (host, entry)]


async def test_oxford_adds_patterns_from_its_linked_phrasal_entry():
    word_path = "/definition/english/rely"
    phrase_path = "/definition/english/rely-on"
    pages = {
        word_path: f"""
            <div id="entryContent"><h1 class="headword">rely</h1>
                <div class="webtop"><span class="pos">verb</span></div>
                <aside class="phrasal_verb_links">
                    <a href="{phrase_path}#sense" title="rely on definition">rely on</a>
                </aside>
            </div>
        """,
        phrase_path: """
            <div id="entryContent"><h1 class="headword">rely on</h1>
                <ul class="examples"><li><span class="cf">rely on somebody for something</span></li></ul>
            </div>
        """,
    }

    def respond(req):
        if req.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /")
        return httpx.Response(200, headers={"Content-Type": "text/html"}, text=pages[req.url.path])

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        (source,) = await search_dictionaries(
            "rely", None, client, source_names=("Oxford Learner’s Dictionaries",)
        )

    assert source.status == "found"
    assert source.excerpt == "rely · 词性 verb · 句型 rely on somebody for something"


@pytest.mark.parametrize("lemma", ["test", "test-drive"])
async def test_cambridge_checks_regular_past_forms_against_the_dictionary(lemma):
    host = "dictionary.cambridge.org"
    pages = {
        "/dictionary/english/test": """
            <div class="entry-body__el"><div class="dpos-h">
                <span class="dhw">test</span><span class="dpos">verb</span>
            </div></div>
        """,
        "/dictionary/english/testing": """
            <meta name="description" content="TESTING definition: 1. difficult: 2. the act of giving tests">
        """,
        "/dictionary/english/tested": (
            '<meta name="description" content="TESTED definition: 1. past simple and past '
            f'participle of {lemma} 2. to put something through a process">'
        ),
    }
    requested = []

    def respond(req):
        if req.url.host != host:
            return httpx.Response(404)
        if req.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /")
        requested.append(req.url.path)
        return httpx.Response(200, headers={"Content-Type": "text/html"}, text=pages[req.url.path])

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        sources = await search_dictionaries("test", None, client)

    source = next(s for s in sources if s.source == "Cambridge Dictionary")
    assert source.status == "found"
    assert set(requested) == set(pages)
    expected = "test · 词性 verb"
    if lemma == "test":
        expected += " · 词形 verb [过去式 tested、过去分词 tested]"
    assert source.excerpt == expected


@pytest.mark.parametrize(
    ("noun_example", "confirmed"),
    [
        ("The doctors have done some <b>tests</b>.", True),
        ("Some tests-based reports arrived.", False),
        ("The test was easy.", False),
    ],
)
def test_cambridge_confirms_a_regular_plural_in_the_matching_noun_examples(noun_example, confirmed):
    entry = CambridgeEntry("test")
    entry.feed(f"""
        <div class="entry-body__el">
            <div class="dpos-h">
                <span class="dhw">test</span><span class="dpos">noun</span>
            </div>
            <div class="dexamp"><span class="deg">{noun_example}</span></div>
        </div>
        <div class="entry-body__el">
            <div class="dpos-h">
                <span class="dhw">test</span><span class="dpos">verb</span>
            </div>
            <div class="dexamp"><span class="deg">I prepared some tests.</span></div>
        </div>
        <div class="entry-body__el">
            <div class="dpos-h">
                <span class="dhw">exam</span><span class="dpos">noun</span>
            </div>
            <div class="dexamp"><span class="deg">I prepared some tests.</span></div>
        </div>
    """)
    expected = "test · 词性 noun、verb"
    if confirmed:
        expected += " · 词形 noun [复数 tests]"
    assert entry.excerpt("test", "") == expected


def test_cambridge_confirms_plural_in_a_partitive_noun_example():
    entry = CambridgeEntry("division")
    entry.feed("""
        <div class="entry-body__el">
            <div class="dpos-h">
                <span class="dhw">division</span><span class="dpos">noun</span>
            </div>
            <div class="dexamp"><span class="deg">
                The team plays in one of the <a>lower</a> divisions.
            </span></div>
        </div>
    """)
    assert entry.excerpt("division", "") == "division · 词性 noun · 词形 noun [复数 divisions]"


def test_cambridge_keeps_alternative_inflections_as_separate_forms():
    entry = CambridgeEntry("maximum")
    entry.feed("""
        <div class="entry-body__el"><div class="dpos-h">
            <span class="dhw">maximum</span><span class="dpos">noun</span>
            <span class="dinfls"><span class="dinfg">
                <span class="dlab">plural</span> <b class="dinf">maxima</b>
                <span class="us"><span class="ipa">ˈmæk.sə.mə</span></span>
                <b class="dinf">maximums</b>
            </span></span>
        </div></div>
        <div class="entry-body__el"><div class="dpos-h">
            <span class="dhw">maximum</span><span class="dpos">noun</span>
            <span class="dinfls">
                <span class="dinfg"><span class="dlab">plural</span> <b class="dinf">maximums</b></span> |
                <span class="dinfg"><b class="dinf">maxima</b></span>
            </span>
        </div></div>
    """)
    assert entry.excerpt("maximum", "") == (
        "maximum · 词性 noun · 词形 noun [复数 maxima、复数 maximums]"
    )


async def test_cambridge_labels_unmarked_irregular_forms_from_their_entries():
    host = "dictionary.cambridge.org"
    pages = {
        "take": """
            <div class="entry-body__el"><div class="dpos-h">
                <span class="dhw">take</span><span class="dpos">verb</span>
                <span class="dinfls">
                    <span class="dinfg"><b class="dinf">took</b></span> |
                    <span class="dinfg"><b class="dinf">taken</b></span>
                </span>
            </div></div>
        """,
        "taking": '<meta name="description" content="TAKING definition: 1. present participle of take">',
        "took": '<meta name="description" content="TOOK definition: 1. past simple of take">',
        "taken": '<meta name="description" content="TAKEN definition: 1. past participle of take">',
    }
    requested = []

    def respond(req):
        if req.url.host != host:
            return httpx.Response(404)
        if req.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /")
        word = req.url.path.rsplit("/", 1)[-1]
        requested.append(word)
        if word not in pages:
            return httpx.Response(404)
        return httpx.Response(200, headers={"Content-Type": "text/html"}, text=pages[word])

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        source = next(
            row
            for row in await search_dictionaries("take", None, client)
            if row.source == "Cambridge Dictionary"
        )
    assert set(requested) == set(pages)
    assert (
        source.excerpt
        == "take · 词性 verb · 词形 verb [现在分词 taking、过去式 took、过去分词 taken]"
    )


async def test_cambridge_keeps_past_labels_from_later_dictionary_sections():
    requested = []
    unmarked = (
        '<div class="entry-body__el"><div class="dpos-h">'
        '<span class="dhw">take</span><span class="dpos">verb</span>'
        '<span class="dinfg"><b class="dinf">took</b></span>'
        '<span class="dinfg"><b class="dinf">taken</b></span>'
        "</div></div>"
    )
    labeled = (
        '<div class="entry-body__el"><div class="dpos-h">'
        '<span class="dhw">take</span><span class="dpos">verb</span>'
        '<span class="dinfg"><span class="dlab">past tense</span> <b class="dinf">took</b></span>'
        '<span class="dinfg"><span class="dlab">past participle</span> <b class="dinf">taken</b></span>'
        "</div></div>"
    )
    take_page = unmarked + " " * 450_000 + labeled

    def respond(req):
        if req.url.host != "dictionary.cambridge.org":
            return httpx.Response(404)
        if req.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /")
        requested.append(req.url.path)
        if req.url.path == "/dictionary/english/take":
            return httpx.Response(200, headers={"Content-Type": "text/html"}, text=take_page)
        return httpx.Response(503)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        source = next(
            row
            for row in await search_dictionaries("take", None, client)
            if row.source == "Cambridge Dictionary"
        )
    assert requested == ["/dictionary/english/take", "/dictionary/english/taking"]
    assert source.excerpt == "take · 词性 verb · 词形 verb [过去式 took、过去分词 taken]"


@pytest.mark.parametrize(
    ("example", "expected"),
    [
        ("This puzzle has well and <a>truly</a> foxed me!", " · 词形 verb [过去分词 foxed]"),
        ("This puzzle had foxed-based clues.", ""),
    ],
)
def test_cambridge_confirms_past_participle_from_a_perfect_verb_example(example, expected):
    entry = CambridgeEntry("fox")
    entry.feed(f"""
        <div class="entry-body__el">
            <div class="dpos-h">
                <span class="dhw">fox</span><span class="dpos">verb</span>
            </div>
            <div class="dexamp"><span class="deg">{example}</span></div>
        </div>
    """)
    assert entry.excerpt("fox", "") == "fox · 词性 verb" + expected


@pytest.mark.parametrize(
    ("examples", "expected"),
    [
        (
            [
                "The manufacturers are currently <b>testing</b> the new engine.",
                "This <a>exercise</a> tests your reflexes.",
            ],
            " · 词形 verb [现在分词 testing、第三人称单数 tests]",
        ),
        (
            [
                "This includes tests for reflexes.",
                "There are tests for reflexes.",
                "She is evaluating testing procedures.",
                "This exercise tests-based chart was updated.",
            ],
            "",
        ),
    ],
)
def test_cambridge_confirms_verb_forms_in_the_matching_verb_examples(examples, expected):
    entry = CambridgeEntry("test")
    verb_examples = "".join(
        f'<div class="dexamp"><span class="deg">{example}</span></div>' for example in examples
    )
    entry.feed(f"""
        <div class="entry-body__el">
            <div class="dpos-h">
                <span class="dhw">test</span><span class="dpos">noun</span>
            </div>
            <div class="dexamp"><span class="deg">This exercise tests your reflexes.</span></div>
        </div>
        <div class="entry-body__el">
            <div class="dpos-h">
                <span class="dhw">test</span><span class="dpos">verb</span>
            </div>
            {verb_examples}
        </div>
        <div class="entry-body__el">
            <div class="dpos-h">
                <span class="dhw">check</span><span class="dpos">verb</span>
            </div>
            <div class="dexamp"><span class="deg">They are testing the engine.</span></div>
        </div>
    """)
    assert entry.excerpt("test", "") == "test · 词性 noun、verb" + expected


def test_cambridge_keeps_grammar_labels_and_excludes_the_forms_ipa_from_headword():
    entry = CambridgeEntry("run")
    entry.feed("""
        <div class="entry-body__el"><div class="dpos-h">
            <span class="dhw">run</span><span class="dpos">verb</span>
            <span class="us"><span class="ipa">rʌn</span></span>
            <span class="dinfls">
                <span class="dinfg">
                    <span class="dlab">present participle</span> <b class="dinf">running</b>
                </span>
                <span class="dinfg">
                    <span class="dlab">past tense</span> <b class="dinf">ran</b>
                    <span class="us">us/<span class="ipa">ræn</span>/</span>
                </span>
                <span class="dinfg">
                    <span class="dlab">past participle</span> <b class="dinf">run</b>
                </span>
            </span>
        </div></div>
    """)
    assert entry.excerpt("run", "") == (
        "run · 美 [rʌn] · 词性 verb · 词形 verb [现在分词 running、过去式 ran、过去分词 run]"
    )
    assert entry.verb_form_candidates(None) == ("run", [])
