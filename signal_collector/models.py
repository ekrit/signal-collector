"""Core data model shared by every scraper and pipeline stage."""

from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass, field
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

_TRACKING_PARAMS = re.compile(r"^(utm_|ref$|ref_|source$|fbclid$|gclid$|mc_)", re.I)


def canonical_url(url: str) -> str:
    """Normalise a URL so the same page scraped from two sources dedupes."""
    if not url:
        return ""
    parts = urlsplit(url.strip())
    query = [(k, v) for k, v in parse_qsl(parts.query) if not _TRACKING_PARAMS.match(k)]
    host = parts.netloc.lower().removeprefix("www.").removeprefix("old.")
    path = parts.path.rstrip("/") or "/"
    return urlunsplit(("https", host, path, urlencode(sorted(query)), ""))


@dataclass
class Signal:
    """One piece of evidence about a (potentially) profitable SaaS idea."""

    source: str                      # e.g. "hackernews", "reddit/r/SaaS"
    source_id: str                   # stable id inside that source
    title: str
    url: str
    text: str = ""
    author: str = ""
    created_at: str = ""             # ISO-8601 UTC
    engagement: dict[str, int] = field(default_factory=dict)  # points, comments...
    kind: str = "idea"               # "idea" | "market_intel"

    # --- enrichment (filled by the pipeline) ---
    mrr_usd: float | None = None     # best monthly-revenue estimate
    revenue_evidence: str = ""       # the text snippet the number came from
    revenue_confidence: float = 0.0  # 0..1, how sure we are it's real revenue
    industries: list[str] = field(default_factory=list)
    halal_status: str = "unknown"    # "ok" | "review" | "excluded" | "unknown"
    halal_reasons: list[str] = field(default_factory=list)
    score: float = 0.0
    llm: dict[str, Any] | None = None
    first_seen: str = ""
    last_seen: str = ""

    @property
    def uid(self) -> str:
        key = canonical_url(self.url) or f"{self.source}:{self.source_id}"
        return hashlib.sha1(key.encode()).hexdigest()[:16]

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["uid"] = self.uid
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Signal":
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in d.items() if k in known})
