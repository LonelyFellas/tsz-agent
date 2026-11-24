import asyncio
from uuid import uuid4

import httpx
import pytest
from pydantic import SecretStr

from tsz_agent.storage import Conflict
from tsz_agent.supervisor import workflow as runtime
from tsz_agent.supervisor.dictionary_sources.cambridge import CambridgeEntry
from tsz_agent.supervisor.dictionary_sources.oxford import OxfordEntry
from tsz_agent.supervisor.lexicon import Input, Material, Scope, SenseContext
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
        )
    else:
        assert source.excerpt == ""


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
