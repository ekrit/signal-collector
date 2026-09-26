"""Git-friendly persistence: sorted JSONL files that produce small, readable diffs."""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterable

from .models import Signal

PRUNE_UNQUALIFIED_AFTER_DAYS = 60
MAX_EXCLUDED = 2000


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def read_jsonl(path: Path) -> list[Signal]:
    if not path.exists():
        return []
    return [Signal.from_dict(json.loads(line)) for line in path.read_text().splitlines() if line.strip()]


def write_jsonl(path: Path, signals: Iterable[Signal]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = sorted(signals, key=lambda s: (s.first_seen, s.uid))
    path.write_text("".join(json.dumps(s.to_dict(), ensure_ascii=False, sort_keys=True) + "\n" for s in rows))


def title_key(s: Signal) -> str | None:
    """Fingerprint for near-duplicate stories syndicated under different URLs."""
    t = re.sub(r"\s+-\s+[^-]{2,40}$", "", s.title)  # drop " - Publisher" suffix (Google News)
    t = re.sub(r"[^a-z0-9$]+", " ", t.lower()).strip()
    return t if len(t) >= 25 and not t.startswith("comment on") else None


def merge(existing: list[Signal], fresh: list[Signal]) -> tuple[dict[str, Signal], int]:
    """Upsert fresh signals. Keeps first_seen and any LLM verdict; refreshes engagement.

    Dedupes on canonical URL, then on title fingerprint (same story, many outlets).
    """
    ts = now_iso()
    by_uid = {s.uid: s for s in existing}
    by_title = {k: s.uid for s in existing if (k := title_key(s))}
    new = 0
    for s in fresh:
        old = by_uid.get(s.uid)
        if old is None and (k := title_key(s)) in by_title:
            old = by_uid[by_title[k]]
        if old is None:
            s.first_seen = s.last_seen = ts
            by_uid[s.uid] = s
            if k := title_key(s):
                by_title[k] = s.uid
            new += 1
            continue
        old.last_seen = ts
        for k, v in s.engagement.items():
            old.engagement[k] = max(v, old.engagement.get(k, 0))
        if len(s.text) > len(old.text):
            old.text = s.text
    return by_uid, new


def prune(signals: dict[str, Signal], keep: Callable[[Signal], bool]) -> dict[str, Signal]:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=PRUNE_UNQUALIFIED_AFTER_DAYS)).isoformat()
    return {u: s for u, s in signals.items() if keep(s) or s.last_seen >= cutoff}
