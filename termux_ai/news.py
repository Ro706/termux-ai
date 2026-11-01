"""RSS/Atom news fetching using httpx + stdlib XML parsing (no extra deps)."""
from __future__ import annotations

import asyncio
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from html import unescape

import httpx

_ATOM = "{http://www.w3.org/2005/Atom}"
_TAG_RE = re.compile(r"<[^>]+>")


@dataclass
class NewsItem:
    title: str
    link: str
    source: str
    published: float  # epoch seconds, 0 if unknown
    summary: str = ""


def _text(el: ET.Element | None) -> str:
    return unescape((el.text or "").strip()) if el is not None else ""


def _parse_date(s: str) -> float:
    if not s:
        return 0.0
    try:
        return parsedate_to_datetime(s).timestamp()
    except (TypeError, ValueError):
        pass
    try:
        from datetime import datetime
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def parse_feed(xml_text: str, fallback_source: str = "") -> list[NewsItem]:
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []
    items: list[NewsItem] = []
    # RSS 2.0
    channel = root.find("channel")
    if channel is not None:
        source = _text(channel.find("title")) or fallback_source
        for it in channel.findall("item"):
            summary = _TAG_RE.sub("", _text(it.find("description")))[:300]
            items.append(NewsItem(_text(it.find("title")), _text(it.find("link")), source, _parse_date(_text(it.find("pubDate"))), summary))
        return [i for i in items if i.title]
    # Atom
    if root.tag == f"{_ATOM}feed":
        source = _text(root.find(f"{_ATOM}title")) or fallback_source
        for e in root.findall(f"{_ATOM}entry"):
            link_el = e.find(f"{_ATOM}link")
            link = link_el.get("href", "") if link_el is not None else ""
            date = _text(e.find(f"{_ATOM}updated")) or _text(e.find(f"{_ATOM}published"))
            summary = _TAG_RE.sub("", _text(e.find(f"{_ATOM}summary")))[:300]
            items.append(NewsItem(_text(e.find(f"{_ATOM}title")), link, source, _parse_date(date), summary))
        return [i for i in items if i.title]
    # RSS 1.0 / RDF
    for it in root.iter():
        if it.tag.endswith("}item"):
            t = next((c for c in it if c.tag.endswith("}title")), None)
            l = next((c for c in it if c.tag.endswith("}link")), None)
            if t is not None:
                items.append(NewsItem(_text(t), _text(l), fallback_source, 0.0))
    return items


async def _fetch_one(client: httpx.AsyncClient, url: str) -> list[NewsItem]:
    try:
        r = await client.get(url)
        r.raise_for_status()
    except httpx.HTTPError:
        return []
    return parse_feed(r.text, fallback_source=httpx.URL(url).host or url)


async def fetch_category(urls: list[str], limit: int = 20) -> list[NewsItem]:
    headers = {"User-Agent": "termux-ai/0.1 (+personal RSS reader)"}
    async with httpx.AsyncClient(timeout=10.0, follow_redirects=True, headers=headers) as client:
        results = await asyncio.gather(*(_fetch_one(client, u) for u in urls))
    merged = [i for group in results for i in group]
    seen, unique = set(), []
    for i in sorted(merged, key=lambda x: x.published, reverse=True):
        key = i.title.lower()[:80]
        if key not in seen:
            seen.add(key)
            unique.append(i)
    return unique[:limit]
