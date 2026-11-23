"""Word-only dictionary search. Only allowed hosts and search-result snippets enter the run."""

import asyncio
from html.parser import HTMLParser
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

import httpx
from pydantic import SecretStr

from .dictionary_sources import cambridge, merriam_webster, oxford, youdao
from .dictionary_sources.source import DictionarySource, EntryReader
from .entry_facts import EntryFacts
from .lexicon import Material

DICTIONARIES = {
    source.name: source
    for source in (youdao.SOURCE, oxford.SOURCE, merriam_webster.SOURCE, cambridge.SOURCE)
}


class ExcerptText(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def plain_excerpt(html: str) -> str:
    parser = ExcerptText()
    parser.feed(html)
    return " ".join("".join(parser.parts).split())[:1400]


class EntrySummary(HTMLParser):
    def __init__(self, word: str) -> None:
        super().__init__()
        self.word = word.casefold()
        self.description = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "meta" or self.description:
            return
        data = dict(attrs)
        if data.get("name") == "description" or data.get("property") == "og:description":
            description = plain_excerpt(data.get("content") or "")
            if self.word in description.casefold():
                self.description = description


async def lookup_entry(
    client: httpx.AsyncClient, source: DictionarySource, item: Material, word: str
) -> Material:
    # Only fixed source hosts and word paths, no arbitrary URLs or cross-site redirects.
    agent = "TSZLexicon/1.0"
    headers = {"User-Agent": agent}
    item.status = "unavailable"
    try:
        async with asyncio.timeout(7):
            parser = RobotFileParser()
            async with client.stream(
                "GET", f"https://{source.host}/robots.txt", headers=headers, follow_redirects=False
            ) as rules:
                if rules.status_code == 200:
                    body = bytearray()
                    async for chunk in rules.aiter_bytes():
                        body.extend(chunk)
                        if len(body) > 256_000:
                            return item
                    parser.parse(body.decode("utf-8", errors="replace").splitlines())
                elif rules.status_code == 404:
                    parser.parse([])
                else:
                    return item

            locator = item.locator
            redirects = 2 if source.valid_redirect else 0
            for hop in range(redirects + 1):
                if not parser.can_fetch(agent, locator):
                    return item
                async with client.stream(
                    "GET", locator, headers=headers, follow_redirects=False
                ) as response:
                    if (
                        response.next_request is not None
                        and source.valid_redirect
                        and hop < redirects
                    ):
                        destination = response.next_request.url
                        if not source.valid_redirect(destination):
                            return item
                        locator = str(destination)
                        continue
                    if response.status_code == 404:
                        item.status = "no_match"
                        return item
                    if response.status_code != 200 or "text/html" not in response.headers.get(
                        "Content-Type", ""
                    ):
                        return item
                    summary, facts = await read_entry_page(response, word, source)
                related = (
                    facts.related_entry(locator, summary) if isinstance(facts, EntryFacts) else None
                )
                excerpt = facts.excerpt(word, summary) if facts else summary
                if excerpt:
                    item.locator, item.status, item.excerpt = locator, "found", excerpt
                related_facts: EntryReader | None = None
                if related and isinstance(facts, EntryFacts):
                    url, related_word = related
                    if parser.can_fetch(agent, url):
                        async with client.stream(
                            "GET", url, headers=headers, follow_redirects=False
                        ) as linked:
                            if linked.status_code == 200 and "text/html" in linked.headers.get(
                                "Content-Type", ""
                            ):
                                _, related_facts = await read_entry_page(
                                    linked, related_word, source
                                )
                                if isinstance(related_facts, EntryFacts):
                                    facts.merge_related(related_facts)
                    enriched = facts.excerpt(word, summary)
                    if enriched:
                        item.locator, item.status, item.excerpt = locator, "found", enriched
                linked_entry = related_facts if isinstance(related_facts, EntryFacts) else None
                if isinstance(facts, cambridge.CambridgeEntry) and (
                    candidate := facts.participle_candidate(linked_entry)
                ):
                    participle, lemma = candidate
                    form_url = source.url(participle)
                    if parser.can_fetch(agent, form_url):
                        async with client.stream(
                            "GET", form_url, headers=headers, follow_redirects=False
                        ) as form_page:
                            if (
                                form_page.status_code == 200
                                and "text/html" in form_page.headers.get("Content-Type", "")
                            ):
                                evidence, _ = await read_entry_page(
                                    form_page, participle, source, summary_only=True
                                )
                                if facts.add_verified_participle(word, participle, lemma, evidence):
                                    item.excerpt = facts.excerpt(word, summary)
                return item
    except (TimeoutError, httpx.HTTPError, ValueError):
        pass
    return item


async def read_entry_page(
    response: httpx.Response, word: str, source: DictionarySource, *, summary_only: bool = False
) -> tuple[str, EntryReader | None]:
    summary = EntrySummary(word)
    facts = source.reader(word) if source.reader and not summary_only else None
    remaining = source.max_page_chars
    async for part in response.aiter_text():
        chunk = part[:remaining]
        summary.feed(chunk)
        if facts:
            facts.feed(chunk)
        remaining -= len(chunk)
        if remaining <= 0 or (facts.finished if facts else bool(summary.description)):
            break
    return summary.description, facts


async def search_dictionaries(
    headword: str,
    key: SecretStr | None,
    client: httpx.AsyncClient,
    *,
    youdao_public_lookup: bool = False,
    merriam_webster_api_key: SecretStr | None = None,
) -> list[Material]:
    async def lookup(source: DictionarySource) -> Material:
        if source is youdao.SOURCE and youdao_public_lookup:
            source = youdao.PUBLIC_SOURCE
        if headword in {".", ".."}:
            return Material(source=source.name, locator="", excerpt="", status="no_match")
        host = source.host
        item = Material(
            source=source.name, locator=source.url(headword), excerpt="", status="not_configured"
        )
        if source is merriam_webster.SOURCE and merriam_webster_api_key:
            return await merriam_webster.lookup_collegiate(
                client, item, headword, merriam_webster_api_key
            )
        item = await lookup_entry(client, source, item, headword)
        if item.status == "found" or not key:
            return item
        try:
            response = await client.get(
                "https://api.search.brave.com/res/v1/web/search",
                params={
                    "q": f'site:{host} "{headword}" dictionary',
                    "count": 4,
                    "text_decorations": "false",
                },
                headers={"X-Subscription-Token": key.get_secret_value()},
                timeout=5,
            )
            response.raise_for_status()
            for hit in response.json().get("web", {}).get("results", []):
                url, snippet = hit.get("url"), hit.get("description")
                if not isinstance(url, str) or len(url) > 1000 or not isinstance(snippet, str):
                    continue
                parts = urlsplit(url)
                if (
                    parts.scheme == "https"
                    and parts.netloc == host
                    and snippet.strip()
                    and headword.casefold()
                    in (str(hit.get("title", "")) + " " + snippet).casefold()
                ):
                    return Material(
                        source=source.name,
                        locator=url,
                        excerpt=plain_excerpt(snippet),
                        status="found",
                    )
        except (httpx.HTTPError, ValueError, AttributeError, TypeError):
            pass
        return item

    return list(await asyncio.gather(*(lookup(source) for source in DICTIONARIES.values())))
