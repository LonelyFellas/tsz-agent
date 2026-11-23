from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

import httpx


class EntryReader(Protocol):
    @property
    def finished(self) -> bool: ...

    def feed(self, data: str) -> None: ...

    def excerpt(self, word: str, summary: str) -> str: ...


@dataclass(frozen=True)
class DictionarySource:
    name: str
    host: str
    url: Callable[[str], str]
    reader: Callable[[str], EntryReader] | None = None
    valid_redirect: Callable[[httpx.URL], bool] | None = None
    max_page_chars: int = 400_000
