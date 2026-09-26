"""Reddit scraper with a three-tier strategy.

1. OAuth app-only API (``REDDIT_CLIENT_ID`` / ``REDDIT_CLIENT_SECRET``) —
   reliable from CI runners, 100 req/min.
2. Public ``.json`` listings — works from most residential IPs.
3. Public ``.rss`` feeds — last resort when JSON is blocked (common for
   datacenter IPs); less metadata but still gets titles + bodies.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any

from ..http import FetchError, HttpClient
from ..models import Signal
from .base import iso_from_ts, parse_feed

log = logging.getLogger(__name__)


class Reddit:
    name = "reddit"

    def __init__(self, cfg: dict[str, Any], lookback_days: int):
        self.subs: list[str] = cfg.get("subreddits", [])
        self.query: str = cfg.get("search_query", "revenue")
        self.window: str = cfg.get("top_window", "week")
        self.limit = int(cfg.get("limit", 100))
        self.since = time.time() - lookback_days * 86400
        self._token: str | None = None
        self.mode = "json"

    async def _auth(self, http: HttpClient) -> None:
        cid, secret = os.getenv("REDDIT_CLIENT_ID"), os.getenv("REDDIT_CLIENT_SECRET")
        if not (cid and secret):
            return
        try:
            tok = await http.post_form("https://www.reddit.com/api/v1/access_token",
                                       {"grant_type": "client_credentials"}, auth=(cid, secret))
            self._token = tok.get("access_token")
            self.mode = "oauth" if self._token else "json"
        except FetchError as e:
            log.warning("reddit oauth failed (%s); falling back to public JSON", e)

    def _base(self) -> str:
        return "https://oauth.reddit.com" if self._token else "https://www.reddit.com"

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"bearer {self._token}"} if self._token else {}

    def _post_to_signal(self, p: dict[str, Any], sub: str) -> Signal:
        return Signal(
            source=f"reddit/r/{sub}", source_id=p["id"], title=p.get("title", ""),
            url="https://www.reddit.com" + p.get("permalink", f"/comments/{p['id']}"),
            text=p.get("selftext") or "", author=p.get("author") or "",
            created_at=iso_from_ts(p.get("created_utc")),
            engagement={"points": int(p.get("score") or 0),
                        "comments": int(p.get("num_comments") or 0),
                        "upvote_ratio_pct": int(100 * float(p.get("upvote_ratio") or 0))})

    async def _listing(self, http: HttpClient, sub: str, path: str, params: dict[str, Any]) -> list[Signal]:
        suffix = "" if self._token else ".json"
        data = await http.get_json(f"{self._base()}/r/{sub}/{path}{suffix}",
                                   params={**params, "raw_json": 1}, headers=self._headers(),
                                   conditional=False)
        posts = [c["data"] for c in data.get("data", {}).get("children", []) if c.get("kind") == "t3"]
        return [self._post_to_signal(p, sub) for p in posts
                if float(p.get("created_utc") or 0) >= self.since and not p.get("stickied")]

    async def _rss(self, http: HttpClient, sub: str) -> list[Signal]:
        out = []
        for url, params in ((f"https://www.reddit.com/r/{sub}/top/.rss", {"t": self.window}),
                            (f"https://www.reddit.com/r/{sub}/search.rss",
                             {"q": self.query, "restrict_sr": 1, "sort": "new"})):
            for e in parse_feed(await http.get(url, params=params, browser=True)):
                pid = e["id"].rsplit("_", 1)[-1] or e["link"]
                out.append(Signal(source=f"reddit/r/{sub}", source_id=pid, title=e["title"],
                                  url=e["link"], text=e["summary"], author=e["author"],
                                  created_at=e["published"]))
        return out

    async def _sub(self, http: HttpClient, sub: str) -> list[Signal]:
        if self.mode != "rss":
            try:
                top, search = await asyncio.gather(
                    self._listing(http, sub, "top", {"t": self.window, "limit": self.limit}),
                    self._listing(http, sub, "search", {"q": self.query, "restrict_sr": 1,
                                                        "sort": "new", "t": "month", "limit": self.limit}))
                return top + search
            except FetchError as e:
                if e.status not in (401, 403, 429):
                    raise
                log.warning("reddit JSON blocked (%s); switching to RSS mode", e.status)
                self.mode = "rss"
        return await self._rss(http, sub)

    async def fetch(self, http: HttpClient) -> list[Signal]:
        await self._auth(http)
        # Probe with the first subreddit so a JSON block flips every other
        # subreddit straight to RSS instead of failing each one.
        results: list[Any] = []
        if self.subs:
            results += await asyncio.gather(self._sub(http, self.subs[0]), return_exceptions=True)
            results += await asyncio.gather(*(self._sub(http, s) for s in self.subs[1:]),
                                            return_exceptions=True)
        out: dict[str, Signal] = {}
        for sub, res in zip(self.subs, results):
            if isinstance(res, Exception):
                log.warning("reddit r/%s failed: %s", sub, res)
                continue
            for s in res:
                out[s.source_id] = s
        log.info("reddit mode=%s, %d posts", self.mode, len(out))
        return list(out.values())
