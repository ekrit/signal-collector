"""Hacker News via the Algolia search API (stories + comments + thread expansion)."""

from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Any

from ..http import FetchError, HttpClient
from ..models import Signal
from .base import iso_from_ts, strip_html

log = logging.getLogger(__name__)
API = "https://hn.algolia.com/api/v1"


class HackerNews:
    name = "hackernews"

    def __init__(self, cfg: dict[str, Any], lookback_days: int):
        self.queries: list[str] = cfg.get("queries", [])
        self.hits = int(cfg.get("hits_per_query", 100))
        self.thread_rx = re.compile(cfg.get("expand_threads_regex", r"$^"))
        self.max_threads = int(cfg.get("max_threads", 5))
        self.since = int(time.time() - lookback_days * 86400)

    def _hit_to_signal(self, h: dict[str, Any]) -> Signal | None:
        is_comment = "comment" in (h.get("_tags") or [])
        oid = str(h.get("objectID") or h.get("id"))
        text = strip_html(h.get("comment_text") or h.get("story_text") or h.get("text") or "")
        title = h.get("title") or h.get("story_title") or ""
        if is_comment:
            title = f"Comment on: {title}" if title else "HN comment"
        if not (title or text):
            return None
        url = h.get("url") if not is_comment else None
        return Signal(
            source="hackernews/comment" if is_comment else "hackernews",
            source_id=oid,
            title=title,
            url=url or f"https://news.ycombinator.com/item?id={oid}",
            text=text,
            author=h.get("author") or "",
            created_at=iso_from_ts(h.get("created_at_i")),
            engagement={"points": int(h.get("points") or 0),
                        "comments": int(h.get("num_comments") or 0)},
        )

    async def _search(self, http: HttpClient, q: str) -> list[dict[str, Any]]:
        params = {"query": q, "tags": "(story,comment)", "hitsPerPage": self.hits,
                  "numericFilters": f"created_at_i>{self.since}"}
        data = await http.get_json(f"{API}/search_by_date", params=params, conditional=False)
        return data.get("hits", [])

    async def _thread(self, http: HttpClient, story_id: str) -> list[Signal]:
        """Flatten a whole thread's comments into signals."""
        item = await http.get_json(f"{API}/items/{story_id}")
        story_title = item.get("title") or ""
        out: list[Signal] = []

        def walk(node: dict[str, Any]) -> None:
            for c in node.get("children") or []:
                txt = strip_html(c.get("text"))
                if txt:
                    out.append(Signal(
                        source="hackernews/thread", source_id=str(c["id"]),
                        title=f"Comment on: {story_title}",
                        url=f"https://news.ycombinator.com/item?id={c['id']}",
                        text=txt, author=c.get("author") or "",
                        created_at=iso_from_ts(c.get("created_at_i")),
                        engagement={"replies": len(c.get("children") or [])}))
                walk(c)
        walk(item)
        return out

    async def fetch(self, http: HttpClient) -> list[Signal]:
        results = await asyncio.gather(*(self._search(http, q) for q in self.queries),
                                       return_exceptions=True)
        signals: dict[str, Signal] = {}
        threads: dict[str, int] = {}
        for q, res in zip(self.queries, results):
            if isinstance(res, Exception):
                log.warning("hn query %r failed: %s", q, res)
                continue
            for h in res:
                s = self._hit_to_signal(h)
                if s:
                    signals[s.source_id] = s
                title = h.get("title") or h.get("story_title") or ""
                sid = str(h.get("story_id") or h.get("objectID"))
                if self.thread_rx.search(title):
                    threads[sid] = max(threads.get(sid, 0), int(h.get("num_comments") or 0))

        top = sorted(threads, key=lambda k: -threads[k])[: self.max_threads]
        expanded = await asyncio.gather(*(self._thread(http, t) for t in top), return_exceptions=True)
        for tid, res in zip(top, expanded):
            if isinstance(res, (FetchError, Exception)):
                log.warning("hn thread %s failed: %s", tid, res)
                continue
            for s in res:
                signals.setdefault(s.source_id, s)
        return list(signals.values())
