"""Polite, resilient async HTTP layer used by every scraper.

Features:
* global concurrency cap + per-host minimum interval (rate limiting)
* retries with exponential backoff + full jitter; honours ``Retry-After``
* conditional requests (ETag / Last-Modified) backed by an on-disk cache, so
  unchanged feeds cost a 304 and no bandwidth between runs
* robots.txt checks for HTML pages (APIs/feeds are opted out explicitly)
* rotating browser User-Agents for HTML, an honest bot UA for APIs
* per-host stats so a run report shows what failed and why
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import random
import time
from collections import defaultdict
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

import httpx

log = logging.getLogger(__name__)

BOT_UA = "signal-collector/1.0 (+https://github.com/ekrit/signal-collector; public research bot)"
BROWSER_UAS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_6) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.6 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64; rv:130.0) Gecko/20100101 Firefox/130.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:130.0) Gecko/20100101 Firefox/130.0",
]
RETRY_STATUSES = {408, 425, 429, 500, 502, 503, 504, 522, 524}


class FetchError(Exception):
    def __init__(self, url: str, status: int | None, msg: str = ""):
        super().__init__(f"{status or 'ERR'} {url} {msg}".strip())
        self.url, self.status = url, status


@dataclass
class HostStats:
    requests: int = 0
    ok: int = 0
    not_modified: int = 0
    retries: int = 0
    errors: dict[str, int] = field(default_factory=lambda: defaultdict(int))


class HttpClient:
    def __init__(
        self,
        cache_dir: str | Path = ".cache/http",
        concurrency: int = 8,
        per_host_interval: float = 1.0,
        max_retries: int = 4,
        timeout: float = 30.0,
        host_intervals: dict[str, float] | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._sem = asyncio.Semaphore(concurrency)
        self._interval = per_host_interval
        self._host_intervals = host_intervals or {}
        self._host_locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._host_next: dict[str, float] = defaultdict(float)
        self._robots: dict[str, RobotFileParser | None] = {}
        self.max_retries = max_retries
        self.stats: dict[str, HostStats] = defaultdict(HostStats)
        self._client = httpx.AsyncClient(
            timeout=timeout, follow_redirects=True, transport=transport,
            headers={"Accept-Language": "en-US,en;q=0.9"},
        )

    async def __aenter__(self) -> "HttpClient":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self._client.aclose()

    # ---------------------------------------------------------------- helpers
    def _cache_path(self, url: str) -> Path:
        return self.cache_dir / (hashlib.sha1(url.encode()).hexdigest() + ".json")

    def _load_cache(self, url: str) -> dict[str, Any] | None:
        p = self._cache_path(url)
        if p.exists():
            try:
                return json.loads(p.read_text())
            except (OSError, ValueError):
                return None
        return None

    def _save_cache(self, url: str, resp: httpx.Response) -> None:
        etag, lm = resp.headers.get("etag"), resp.headers.get("last-modified")
        if not (etag or lm):
            return
        self._cache_path(url).write_text(json.dumps(
            {"etag": etag, "last_modified": lm, "body": resp.text, "saved": time.time()}))

    async def _throttle(self, host: str) -> None:
        interval = self._host_intervals.get(host, self._interval)
        async with self._host_locks[host]:
            wait = self._host_next[host] - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            self._host_next[host] = time.monotonic() + interval * random.uniform(1.0, 1.3)

    @staticmethod
    def _retry_after(resp: httpx.Response) -> float | None:
        val = resp.headers.get("retry-after")
        if not val:
            return None
        try:
            return float(val)
        except ValueError:
            try:
                return max(0.0, parsedate_to_datetime(val).timestamp() - time.time())
            except (TypeError, ValueError):
                return None

    async def allowed_by_robots(self, url: str) -> bool:
        parts = urlsplit(url)
        base = f"{parts.scheme}://{parts.netloc}"
        if base not in self._robots:
            rp: RobotFileParser | None = RobotFileParser()
            try:
                r = await self._client.get(base + "/robots.txt", headers={"User-Agent": BOT_UA})
                if r.status_code >= 400:
                    rp = None  # no robots.txt ⇒ allowed
                else:
                    rp.parse(r.text.splitlines())
            except httpx.HTTPError:
                rp = None
            self._robots[base] = rp
        rp = self._robots[base]
        return True if rp is None else rp.can_fetch(BOT_UA, url)

    # -------------------------------------------------------------------- API
    async def get(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        browser: bool = False,
        check_robots: bool = False,
        conditional: bool = True,
    ) -> str:
        """GET ``url`` and return the body text (from cache on HTTP 304)."""
        full = str(httpx.URL(url, params=params)) if params else url
        host = urlsplit(full).netloc
        st = self.stats[host]

        if check_robots and not await self.allowed_by_robots(full):
            st.errors["robots_disallowed"] += 1
            raise FetchError(full, None, "disallowed by robots.txt")

        hdrs = {"User-Agent": random.choice(BROWSER_UAS) if browser else BOT_UA}
        if browser:
            hdrs["Accept"] = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
        hdrs.update(headers or {})
        cached = self._load_cache(full) if conditional else None
        if cached:
            if cached.get("etag"):
                hdrs["If-None-Match"] = cached["etag"]
            if cached.get("last_modified"):
                hdrs["If-Modified-Since"] = cached["last_modified"]

        last_exc: Exception | None = None
        for attempt in range(self.max_retries + 1):
            await self._throttle(host)
            async with self._sem:
                st.requests += 1
                try:
                    resp = await self._client.get(full, headers=hdrs)
                except httpx.HTTPError as e:
                    last_exc = e
                    st.errors[type(e).__name__] += 1
                    resp = None
            if resp is not None:
                if resp.status_code == 304 and cached:
                    st.not_modified += 1
                    return cached["body"]
                if resp.status_code < 400:
                    st.ok += 1
                    if conditional:
                        self._save_cache(full, resp)
                    return resp.text
                st.errors[str(resp.status_code)] += 1
                if resp.status_code not in RETRY_STATUSES:
                    raise FetchError(full, resp.status_code)
                last_exc = FetchError(full, resp.status_code)
                delay = self._retry_after(resp)
            else:
                delay = None
            if attempt == self.max_retries:
                break
            st.retries += 1
            backoff = delay if delay is not None else random.uniform(0, min(60, 2 ** (attempt + 1)))
            log.debug("retry %s in %.1fs (attempt %d)", full, backoff, attempt + 1)
            await asyncio.sleep(min(backoff, 120))
        if isinstance(last_exc, FetchError):
            raise last_exc
        raise FetchError(full, None, repr(last_exc))

    async def get_json(self, url: str, **kw: Any) -> Any:
        return json.loads(await self.get(url, **kw))

    async def post_form(self, url: str, data: dict[str, str], auth: tuple[str, str] | None = None) -> Any:
        """Single POST (used for OAuth token exchange); returns parsed JSON."""
        host = urlsplit(url).netloc
        await self._throttle(host)
        self.stats[host].requests += 1
        resp = await self._client.post(url, data=data, auth=auth, headers={"User-Agent": BOT_UA})
        if resp.status_code >= 400:
            self.stats[host].errors[str(resp.status_code)] += 1
            raise FetchError(url, resp.status_code)
        self.stats[host].ok += 1
        return resp.json()

    def stats_report(self) -> dict[str, Any]:
        return {h: {"requests": s.requests, "ok": s.ok, "not_modified": s.not_modified,
                    "retries": s.retries, "errors": dict(s.errors)}
                for h, s in sorted(self.stats.items())}
