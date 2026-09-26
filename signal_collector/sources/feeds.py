"""Feed/API sources: dev.to, Product Hunt, generic RSS, Google News."""

from __future__ import annotations

import asyncio
import logging
import re
import time
from datetime import datetime
from typing import Any

from ..classify import Taxonomy
from ..http import HttpClient
from ..models import Signal
from .base import parse_feed

log = logging.getLogger(__name__)


def _recent(iso: str, since: float) -> bool:
    if not iso:
        return True  # undated: keep, dedupe handles repeats
    try:
        return datetime.fromisoformat(iso).timestamp() >= since
    except ValueError:
        return True


async def _gather_logged(label: str, coros: list[Any]) -> list[Any]:
    out = []
    for res in await asyncio.gather(*coros, return_exceptions=True):
        if isinstance(res, Exception):
            log.warning("%s: %s", label, res)
        else:
            out.append(res)
    return out


class DevTo:
    name = "devto"
    API = "https://dev.to/api"

    def __init__(self, cfg: dict[str, Any], lookback_days: int):
        self.tags = cfg.get("tags", [])
        self.deep_rx = re.compile(cfg.get("deep_fetch_regex", r"$^"))
        self.max_deep = int(cfg.get("max_deep_fetch", 30))
        self.days = lookback_days

    async def fetch(self, http: HttpClient) -> list[Signal]:
        lists = await _gather_logged("devto", [
            http.get_json(f"{self.API}/articles", params={"tag": t, "per_page": 100, "top": self.days},
                          conditional=False) for t in self.tags])
        arts = {a["id"]: a for lst in lists for a in lst}
        # Articles whose title/description hint at revenue get their full body.
        deep = [a for a in arts.values()
                if self.deep_rx.search(f"{a.get('title')} {a.get('description')}")][: self.max_deep]
        bodies = await asyncio.gather(*(http.get_json(f"{self.API}/articles/{a['id']}") for a in deep),
                                      return_exceptions=True)
        full = {a["id"]: b.get("body_markdown", "") for a, b in zip(deep, bodies) if isinstance(b, dict)}
        return [Signal(
            source="devto", source_id=str(a["id"]), title=a.get("title", ""), url=a.get("url", ""),
            text=full.get(a["id"]) or a.get("description") or "",
            author=(a.get("user") or {}).get("username", ""),
            created_at=a.get("published_timestamp") or "",
            engagement={"points": int(a.get("public_reactions_count") or 0),
                        "comments": int(a.get("comments_count") or 0)}) for a in arts.values()]


class FeedSource:
    """Any list of RSS/Atom feeds (Product Hunt, Medium tags, hnrss, newsletters...)."""

    def __init__(self, name: str, feeds: dict[str, str] | list[str], lookback_days: int,
                 kind: str = "idea"):
        self.name = name
        self.feeds = feeds if isinstance(feeds, dict) else {f"{name}_{i}": u for i, u in enumerate(feeds)}
        self.since = time.time() - lookback_days * 86400
        self.kind = kind

    async def _one(self, http: HttpClient, label: str, url: str) -> list[Signal]:
        entries = parse_feed(await http.get(url, browser=True))
        return [Signal(source=f"{self.name}/{label}", source_id=e["id"], title=e["title"],
                       url=e["link"], text=e["summary"][:20000], author=e["author"],
                       created_at=e["published"], kind=self.kind)
                for e in entries if _recent(e["published"], self.since)]

    async def fetch(self, http: HttpClient) -> list[Signal]:
        lists = await _gather_logged(self.name, [self._one(http, k, u) for k, u in self.feeds.items()])
        return [s for lst in lists for s in lst]


class GoogleNews(FeedSource):
    """Google News RSS search: revenue stories + per-industry market intelligence."""

    URL = "https://news.google.com/rss/search"

    def __init__(self, cfg: dict[str, Any], lookback_days: int, taxonomy: Taxonomy):
        super().__init__("google_news", {}, lookback_days)
        self.queries: list[tuple[str, str, str]] = [
            (f"idea_{i}", q, "idea") for i, q in enumerate(cfg.get("idea_queries", []))]
        tmpl = cfg.get("market_query_template")
        if tmpl:
            self.queries += [(f"market_{k}", tmpl.format(name=ind.name.split(" (")[0].split(" & ")[0]),
                              "market_intel") for k, ind in taxonomy.industries.items()]
        self.days = lookback_days

    async def _q(self, http: HttpClient, label: str, q: str, kind: str) -> list[Signal]:
        xml = await http.get(self.URL, params={"q": f"{q} when:{self.days}d", "hl": "en-US",
                                               "gl": "US", "ceid": "US:en"}, browser=True)
        return [Signal(source=f"google_news/{label}", source_id=e["id"], title=e["title"],
                       url=e["link"], text=e["summary"], created_at=e["published"], kind=kind)
                for e in parse_feed(xml) if _recent(e["published"], self.since)]

    async def fetch(self, http: HttpClient) -> list[Signal]:
        lists = await _gather_logged(self.name, [self._q(http, *q) for q in self.queries])
        return [s for lst in lists for s in lst]
