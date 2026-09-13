"""Web search backends. Kimi's own SearchWeb needs the Kimi platform service,
so for a third-party OpenAI-compatible provider we supply our own.

Backends, in order of preference:
  1. SERPER_API_KEY  -> https://google-search3.p.rapidapi.com style JSON API (serper.dev)
  2. KEYWORD_SEARCH_URL -> a template URL, {query} placeholder, returns HTML or JSON
  3. duckduckgo html lite scrape, no key required
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.parse
from typing import Any

import aiohttp

UA = "Mozilla/5.0 (X11; Linux x86_64) coomi-kimi-agent/0.1"

# One budget for the whole call. Three backends with their own 25s timeouts
# would otherwise stack into 75 seconds of hanging when the network is simply
# unreachable (which is the common case on a phone).
BUDGET_SECONDS = float(os.environ.get("COOMI_SEARCH_TIMEOUT", "20"))


def _timeout(remaining: float) -> aiohttp.ClientTimeout:
    return aiohttp.ClientTimeout(
        total=max(remaining, 0.5),
        connect=min(8.0, max(remaining, 0.5)),
        sock_read=min(10.0, max(remaining, 0.5)),
    )


async def search(query: str, limit: int = 6) -> Any:
    deadline = time.monotonic() + BUDGET_SECONDS

    def left() -> float:
        return deadline - time.monotonic()

    limit = max(1, min(int(limit or 6), 15))
    key = os.environ.get("SERPER_API_KEY", "").strip()
    if key and left() > 1:
        hits = await _serper(query, limit, key, left())
        if hits is not None:
            return hits
    template = os.environ.get("KEYWORD_SEARCH_URL", "").strip()
    if template and left() > 1:
        hits = await _template(query, limit, template, left())
        if hits is not None:
            return hits
    hits = await _duckduckgo(query, limit, left()) if left() > 1 else None
    if hits is not None:
        return hits
    return (
        "web_search found nothing: no backend answered (no SERPER_API_KEY / "
        "KEYWORD_SEARCH_URL set, and the network is unreachable). The FetchURL "
        "tool still works for URLs you already know."
    )


async def _post_json(url: str, payload: dict[str, Any], headers: dict[str, str], limit: int,
                 budget: float = BUDGET_SECONDS) -> Any:
    try:
        async with aiohttp.ClientSession(timeout=_timeout(budget)) as session:
            async with session.post(url, json=payload, headers=headers) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json(content_type=None)
    except (aiohttp.ClientError, TimeoutError, json.JSONDecodeError, ValueError):
        return None
    results = data.get("web") or data.get("results") or []
    hits = [
        {"title": r.get("title", ""), "url": r.get("link") or r.get("url", ""),
         "snippet": (r.get("snippet") or "")[:400]}
        for r in results[:limit]
        if (r.get("link") or r.get("url"))
    ]
    return hits or None


async def _serper(query: str, limit: int, key: str, budget: float = BUDGET_SECONDS) -> Any:
    return await _post_json(
        "https://google.serper.dev/search",
        {"q": query, "num": limit},
        {"X-API-KEY": key, "Content-Type": "application/json", "User-Agent": UA},
        limit, budget,
    )


async def _template(query: str, limit: int, template: str,
                budget: float = BUDGET_SECONDS) -> Any:
    url = template.replace("{query}", urllib.parse.quote(query))
    try:
        async with aiohttp.ClientSession(timeout=_timeout(budget)) as session:
            async with session.get(url, headers={"User-Agent": UA}) as resp:
                if resp.status != 200:
                    return None
                body = await resp.text(errors="replace")
    except (aiohttp.ClientError, TimeoutError, ValueError):
        return None
    try:
        data = json.loads(body)
        return _extract_results(data, limit)
    except json.JSONDecodeError:
        return None


def _extract_results(data: Any, limit: int) -> Any:
    if isinstance(data, list):
        items = data
    elif isinstance(data, dict):
        items = data.get("results") or data.get("web") or data.get("items") or []
    else:
        items = []
    hits = [
        {"title": str(r.get("title", "")), "url": str(r.get("url") or r.get("link") or ""),
         "snippet": str(r.get("snippet") or r.get("content") or "")[:400]}
        for r in items[:limit] if isinstance(r, dict)
    ]
    return hits or None


async def _duckduckgo(query: str, limit: int, budget: float = BUDGET_SECONDS) -> Any:
    """DuckDuckGo lite endpoint; no key. Returns None when the network is blocked."""
    url = "https://html.duckduckgo.com/html/"
    try:
        async with aiohttp.ClientSession(timeout=_timeout(budget)) as session:
            async with session.post(
                url, data={"q": query},
                headers={"User-Agent": UA, "Content-Type": "application/x-www-form-urlencoded"},
            ) as resp:
                if resp.status != 200:
                    return None
                body = await resp.text(errors="replace")
    except (aiohttp.ClientError, TimeoutError, ValueError):
        return None

    hits: list[dict[str, str]] = []
    for block in re.findall(
        r'<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>.*?'
        r'(?:<a[^>]+class="result__snippet"[^>]*>(.*?)</a>)?',
        body,
        flags=re.S,
    ):
        href, title, snippet = block
        real = _ddg_redirect(href)
        if not real:
            continue
        hits.append({
            "title": _strip_tags(title),
            "url": real,
            "snippet": _strip_tags(snippet)[:400],
        })
        if len(hits) >= limit:
            break
    return hits or None


def _ddg_redirect(href: str) -> str:
    match = re.search(r"[?&]uddg=([^&]+)", href or "")
    if match:
        return urllib.parse.unquote(match.group(1))
    return href if href.startswith("http") else ""


def _strip_tags(text: str) -> str:
    clean = re.sub(r"<[^>]+>", "", text or "")
    return re.sub(r"\s+", " ", clean).strip()
