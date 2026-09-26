"""Source registry: builds every enabled scraper from ``config/sources.yaml``."""

from __future__ import annotations

from typing import Any

from ..classify import Taxonomy
from .base import Source
from .feeds import DevTo, FeedSource, GoogleNews
from .hackernews import HackerNews
from .reddit import Reddit


def build_sources(cfg: dict[str, Any], taxonomy: Taxonomy, only: set[str] | None = None) -> list[Source]:
    days = int(cfg.get("lookback_days", 7))
    on = lambda key: cfg.get(key, {}).get("enabled", False) and (not only or key in only)  # noqa: E731
    out: list[Source] = []
    if on("hackernews"):
        out.append(HackerNews(cfg["hackernews"], days))
    if on("reddit"):
        out.append(Reddit(cfg["reddit"], days))
    if on("devto"):
        out.append(DevTo(cfg["devto"], days))
    if on("producthunt"):
        out.append(FeedSource("producthunt", cfg["producthunt"]["feeds"], days))
    if on("rss"):
        out.append(FeedSource("rss", cfg["rss"]["feeds"], days))
    if on("google_news"):
        out.append(GoogleNews(cfg["google_news"], days, taxonomy))
    return out
