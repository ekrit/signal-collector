"""Entry point: ``python -m signal_collector [--sources hackernews,reddit] [--no-llm]``."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path

import yaml

from . import llm, report, store
from .classify import CONFIG_DIR, Taxonomy
from .http import HttpClient
from .models import Signal
from .pipeline import apply_llm, enrich, is_qualified, score
from .sources import build_sources

log = logging.getLogger("signal_collector")

# Seconds between requests to the same host (default 1s for everything else).
HOST_INTERVALS = {"www.reddit.com": 2.0, "oauth.reddit.com": 0.7, "news.google.com": 1.5, "medium.com": 1.5}


async def collect(cfg: dict, tax: Taxonomy, only: set[str] | None, cache_dir: str) -> tuple[list[Signal], dict]:
    sources = build_sources(cfg, tax, only)
    run: dict = {"sources": {}}
    async with HttpClient(cache_dir=cache_dir, host_intervals=HOST_INTERVALS) as http:
        async def one(src):
            t0 = time.monotonic()
            try:
                sigs = await asyncio.wait_for(src.fetch(http), timeout=15 * 60)
                run["sources"][src.name] = {"count": len(sigs), "seconds": round(time.monotonic() - t0, 1)}
                return sigs
            except Exception as e:  # one broken source must never kill the run
                log.exception("source %s failed", src.name)
                run["sources"][src.name] = {"count": 0, "error": f"{type(e).__name__}: {e}"[:300]}
                return []
        results = await asyncio.gather(*(one(s) for s in sources))
        run["http"] = http.stats_report()
    return [s for lst in results for s in lst], run


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--sources", help="comma-separated subset of sources to run")
    ap.add_argument("--no-llm", action="store_true", help="skip Claude review even if a key is set")
    ap.add_argument("--llm-max", type=int, default=40, help="max signals reviewed by Claude per run")
    ap.add_argument("--data-dir", default="data")
    ap.add_argument("--cache-dir", default=".cache/http")
    ap.add_argument("--report-only", action="store_true", help="rebuild reports from stored data")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    data = Path(a.data_dir)
    tax = Taxonomy()
    cfg = yaml.safe_load((CONFIG_DIR / "sources.yaml").read_text())
    ideas_path, intel_path, excl_path = data / "signals.jsonl", data / "market_intel.jsonl", data / "excluded.jsonl"
    existing, intel_old, excl_old = (store.read_jsonl(p) for p in (ideas_path, intel_path, excl_path))

    run: dict = {"started": store.now_iso(), "sources": {}}
    fresh: list[Signal] = []
    if not a.report_only:
        only = set(a.sources.split(",")) if a.sources else None
        fresh, run = asyncio.run(collect(cfg, tax, only, a.cache_dir))
        run["started"] = store.now_iso()
    for s in fresh:
        enrich(s, tax)

    # Route: market intel / excluded (audit trail) / idea signals worth tracking.
    intel_new = [s for s in fresh if s.kind == "market_intel"]
    excluded_new = [s for s in fresh if s.kind == "idea" and s.halal_status == "excluded"]
    keep_new = [s for s in fresh if s.kind == "idea" and s.halal_status != "excluded"
                and (s.mrr_usd or s.industries)]

    signals, n_new = store.merge(existing, keep_new)
    intel, _ = store.merge(intel_old, intel_new)
    excluded, _ = store.merge(excl_old, excluded_new)

    for s in signals.values():
        enrich(s, tax)  # re-apply rules (config may have changed); LLM verdicts win
        apply_llm(s, tax)
        s.score = score(s, tax)

    if not a.no_llm and llm.enabled():
        todo = sorted((s for s in signals.values() if s.llm is None and s.halal_status != "excluded"
                       and (s.mrr_usd or s.score >= 30)), key=lambda s: -s.score)
        done = asyncio.run(llm.review(todo, tax, max_items=a.llm_max))
        run["llm_reviewed"] = done
        for s in todo[: a.llm_max]:
            apply_llm(s, tax)
            s.score = score(s, tax)

    signals = store.prune(signals, is_qualified)
    intel = store.prune(intel, lambda s: False)
    store.write_jsonl(ideas_path, signals.values())
    store.write_jsonl(intel_path, intel.values())
    store.write_jsonl(excl_path, sorted(excluded.values(), key=lambda s: s.last_seen)[-store.MAX_EXCLUDED:])

    run.update(new=n_new, fetched=len(fresh), tracked=len(signals), finished=store.now_iso())
    res = report.build(list(signals.values()), list(intel.values()), tax, run, Path("reports"))
    qualified = res["ideas"]
    (data / "ideas.json").write_text(json.dumps(
        [{"rank": i, **s.to_dict()} for i, s in enumerate(qualified, 1)], indent=1, ensure_ascii=False))
    (data / "industries.json").write_text(json.dumps(res["board"], indent=1))

    if not a.report_only:
        day = datetime.now(timezone.utc)
        runs = data / "runs" / day.strftime("%Y-%m")
        runs.mkdir(parents=True, exist_ok=True)
        (runs / f"{day:%d-%H%M%S}.json").write_text(json.dumps(run, indent=1))
        trends = data / "trends.csv"
        if not trends.exists():
            trends.write_text("date,industry,tracked_signals,qualified_ideas,median_mrr\n")
        with trends.open("a") as f:
            for r in res["board"]:
                total = sum(1 for s in signals.values() if s.industries[:1] == [r["key"]])
                f.write(f"{day:%Y-%m-%d},{r['key']},{total},{r['qualified_ideas']},{r['median_mrr'] or ''}\n")

    log.info("fetched=%d new=%d tracked=%d qualified=%d", len(fresh), n_new, len(signals), len(qualified))
    return 0
