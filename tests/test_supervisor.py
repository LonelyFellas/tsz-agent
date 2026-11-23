import asyncio
from uuid import uuid4

import httpx
import pytest
from pydantic import SecretStr

from tsz_agent.storage import Conflict
from tsz_agent.supervisor import workflow as runtime
from tsz_agent.supervisor.dictionary_sources.cambridge import CambridgeEntry
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
    assert [s.status for s in sources] == ["no_match", "unavailable", "no_match", "found"]
    assert sources[-1].excerpt == "Tomorrow: the next day."


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
        "www.youdao.com",
        "www.oxfordlearnersdictionaries.com",
        "dictionary.cambridge.org",
    }
    assert "www.dictionaryapi.com" in caplog.text
    assert key not in caplog.text


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
    assert "dictionary.cambridge.org" not in indexed_hosts
    assert materials[-1].excerpt == "Tomorrow from the live dictionary."
    assert all(
        m.excerpt == "The next day." and m.status == "found" and "example.org" not in m.locator
        for m in materials[:-1]
    )


@pytest.mark.parametrize(
    ("headword", "rules", "expected"),
    [
        ("center", "Allow: /", "found"),
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
                  <h2><span class="keyword">center</span>
                    <div><span class="pronounce">英 <span>[ˈsentə]</span></span>
                    <span class="pronounce">美 <span>[ˈsentər]</span></span></div>
                  </h2>
                  <div><ul><li>n. Sample text</li><li>v. Sample text</li></ul>
                  <p class="additional">[复数 centers]</p></div>
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
            source.excerpt == "center · 英 [ˈsentə] / 美 [ˈsentər] · 词性 n.、v. · [复数 centers]"
        )
    else:
        assert source.excerpt == ""


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
    assert entry.participle_candidate(None) is None
