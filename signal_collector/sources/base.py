"""Shared helpers for source scrapers."""

from __future__ import annotations

import html
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Protocol

from ..http import HttpClient
from ..models import Signal

_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"[ \t\r\f\v]+")


class Source(Protocol):
    name: str

    async def fetch(self, http: HttpClient) -> list[Signal]: ...


def strip_html(s: str | None) -> str:
    if not s:
        return ""
    s = re.sub(r"(?i)<(br|/p|/div|/li|p)[^>]*>", "\n", s)
    s = html.unescape(_TAG.sub(" ", s))
    return "\n".join(_WS.sub(" ", line).strip() for line in s.splitlines() if line.strip())


def iso_from_ts(ts: float | int | None) -> str:
    if not ts:
        return ""
    return datetime.fromtimestamp(float(ts), tz=timezone.utc).isoformat(timespec="seconds")


def iso_from_any(s: str | None) -> str:
    """Parse RFC-822 (RSS) or ISO-8601 (Atom/JSON) dates into ISO-8601 UTC."""
    if not s:
        return ""
    s = s.strip()
    for parse in (parsedate_to_datetime, lambda v: datetime.fromisoformat(v.replace("Z", "+00:00"))):
        try:
            dt = parse(s)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc).isoformat(timespec="seconds")
        except (TypeError, ValueError, IndexError):
            continue
    return ""


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def parse_feed(xml_text: str) -> list[dict[str, Any]]:
    """Parse RSS 2.0 or Atom into ``[{title, link, summary, published, author, id}]``."""
    try:
        root = ET.fromstring(xml_text.encode() if isinstance(xml_text, str) else xml_text)
    except ET.ParseError:
        return []
    items = [el for el in root.iter() if _local(el.tag) in ("item", "entry")]
    out = []
    for it in items:
        d: dict[str, Any] = {"title": "", "link": "", "summary": "", "published": "", "author": "", "id": ""}
        for child in it:
            name = _local(child.tag)
            text = (child.text or "").strip()
            if name == "title":
                d["title"] = strip_html(text)
            elif name == "link":
                href = child.attrib.get("href")
                rel = child.attrib.get("rel", "alternate")
                if href and rel == "alternate":
                    d["link"] = href
                elif text and not d["link"]:
                    d["link"] = text
            elif name in ("description", "summary", "content", "encoded"):
                body = text or "".join(child.itertext())
                if len(body) > len(d["summary"]):
                    d["summary"] = body
            elif name in ("pubdate", "published", "updated", "date") and not d["published"]:
                d["published"] = iso_from_any(text)
            elif name in ("author", "creator"):
                d["author"] = text or " ".join(t.strip() for t in child.itertext() if t.strip())
            elif name in ("guid", "id"):
                d["id"] = text
        d["summary"] = strip_html(d["summary"])
        d["id"] = d["id"] or d["link"]
        out.append(d)
    return out
