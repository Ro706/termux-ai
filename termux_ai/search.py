"""Web search via DuckDuckGo's HTML endpoint (no API key, no extra deps)."""
from __future__ import annotations

import re
from dataclasses import dataclass
from html import unescape
from urllib.parse import parse_qs, unquote, urlparse

import httpx

_RESULT_RE = re.compile(
    r'<a[^>]+class="result__a"[^>]+href="(?P<href>[^"]+)"[^>]*>(?P<title>.*?)</a>.*?'
    r'(?:<a[^>]+class="result__snippet"[^>]*>(?P<snippet>.*?)</a>)?',
    re.S,
)
_TAG_RE = re.compile(r"<[^>]+>")


@dataclass
class SearchResult:
    title: str
    url: str
    snippet: str


def _clean(s: str | None) -> str:
    return unescape(_TAG_RE.sub("", s or "")).strip()


def _real_url(href: str) -> str:
    if href.startswith("//"):
        href = "https:" + href
    q = parse_qs(urlparse(href).query)
    return unquote(q["uddg"][0]) if "uddg" in q else href


async def web_search(query: str, limit: int = 6) -> list[SearchResult]:
    headers = {"User-Agent": "Mozilla/5.0 (Linux; Android 14) termux-ai/0.1"}
    async with httpx.AsyncClient(timeout=12.0, follow_redirects=True, headers=headers) as client:
        r = await client.post("https://html.duckduckgo.com/html/", data={"q": query})
        r.raise_for_status()
    out = []
    for m in _RESULT_RE.finditer(r.text):
        url = _real_url(m.group("href"))
        if "duckduckgo.com/y.js" in url:  # ads
            continue
        out.append(SearchResult(_clean(m.group("title")), url, _clean(m.group("snippet"))))
        if len(out) >= limit:
            break
    return out
