"""Markdown + JSON reports built from the stored signals."""

from __future__ import annotations

import json
import statistics
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .classify import Taxonomy
from .models import Signal
from .pipeline import is_qualified

HALAL_ICON = {"ok": "✅", "review": "⚠️", "excluded": "⛔", "unknown": "·"}


def _cell(s: Any, n: int = 90) -> str:
    s = " ".join(str(s or "").split()).replace("|", "\\|")
    return s if len(s) <= n else s[: n - 1] + "…"


def _money(v: float | None) -> str:
    if not v:
        return "—"
    return f"${v / 1e6:.1f}M" if v >= 1e6 else f"${v / 1e3:.1f}k" if v >= 1e3 else f"${v:.0f}"


def _label(s: Signal) -> str:
    idea = (s.llm or {}).get("idea")
    if not idea and s.title.startswith("Comment on:"):
        idea = f"💬 {s.text}"  # comments: the comment is the idea, not the thread title
    return _cell(idea or s.title, 80)


def _ind(s: Signal, tax: Taxonomy) -> str:
    return ", ".join(tax.industries[k].name.split(" (")[0] for k in s.industries[:2] if k in tax.industries) or "—"


def _recent(sigs: list[Signal], days: int) -> list[Signal]:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    return [s for s in sigs if s.first_seen >= cutoff]


def build(signals: list[Signal], intel: list[Signal], tax: Taxonomy,
          run: dict[str, Any], out_dir: Path) -> dict[str, Any]:
    ideas = sorted((s for s in signals if is_qualified(s)), key=lambda s: -s.score)
    emerging = sorted((s for s in signals if not is_qualified(s) and s.halal_status != "excluded"
                       and s.industries and any(tax.is_hypergrowth(k) for k in s.industries)),
                      key=lambda s: -s.score)
    review = [s for s in ideas if s.halal_status == "review"]

    # --- per-industry leaderboard
    by_ind: dict[str, list[Signal]] = defaultdict(list)
    for s in ideas:
        for k in s.industries[:1]:
            by_ind[k].append(s)
    vol7 = Counter(k for s in _recent(signals, 7) for k in s.industries[:1])
    board = []
    for row in tax.summary():
        k = row["key"]
        mrrs = [s.mrr_usd for s in by_ind.get(k, []) if s.mrr_usd]
        board.append({**row, "qualified_ideas": len(by_ind.get(k, [])),
                      "median_mrr": statistics.median(mrrs) if mrrs else None,
                      "signals_7d": vol7.get(k, 0)})

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    L: list[str] = [
        "# SaaS Opportunity Report", "",
        f"_Last updated {now} · {len(signals)} tracked signals · {len(ideas)} qualified ideas "
        f"(≥ $1k/month, passed Islamic-values screen) · {run.get('new', 0)} new this run_", "",
        "Scores weigh proven revenue, industry growth (4x-in-5-years target), community traction "
        "and halal-economy fit. ✅ = passes screen · ⚠️ = needs your review.", "",
        "## Target industries", "",
        "| Industry | Market now | Forecast | CAGR | 5-yr multiple | Qualified ideas | Median MRR | Signals (7d) |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for r in board:
        star = " 🚀" if r["hypergrowth"] else ""
        hal = " ☪️" if r["halal_economy"] else ""
        L.append(f"| [{r['name']}]({r['source'] if r['source'].startswith('http') else '#'}){star}{hal} "
                 f"| ${r['market_usd_bn']:g}B ({r['market_year']}) | ${r['target_usd_bn']:g}B ({r['target_year']}) "
                 f"| {r['cagr'] * 100:.1f}% | {r['growth_5y']}x | {r['qualified_ideas']} "
                 f"| {_money(r['median_mrr'])} | {r['signals_7d']} |")
    L += ["", "🚀 = on track to grow ≥ 4x within 5 years · ☪️ = halal-economy vertical", ""]

    L += ["## Top revenue-proven ideas", "",
          "| # | Score | Idea | MRR | Industry | Halal | Source | Seen |", "|---|---|---|---|---|---|---|---|"]
    for i, s in enumerate(ideas[:50], 1):
        L.append(f"| {i} | {s.score:.0f} | [{_label(s)}]({s.url}) | {_money(s.mrr_usd)} | {_ind(s, tax)} "
                 f"| {HALAL_ICON[s.halal_status]} | {s.source} | {s.first_seen[:10]} |")
    if not ideas:
        L.append("| | | _No qualified ideas yet — the first scheduled run will fill this in._ | | | | | |")

    L += ["", "## Emerging in hypergrowth industries (no revenue proof yet)", "",
          "| Score | Signal | Industry | Traction | Source |", "|---|---|---|---|---|"]
    for s in emerging[:25]:
        tr = f"{s.engagement.get('points', 0)}▲ {s.engagement.get('comments', 0)}💬"
        L.append(f"| {s.score:.0f} | [{_label(s)}]({s.url}) | {_ind(s, tax)} | {tr} | {s.source} |")

    if review:
        L += ["", "## Needs Islamic-values review", ""]
        L += [f"- [{_label(s)}]({s.url}) — {_cell('; '.join(s.halal_reasons), 140)}" for s in review[:30]]

    L += ["", "## Latest market intelligence", ""]
    intel_by: dict[str, list[Signal]] = defaultdict(list)
    for s in sorted(intel, key=lambda s: s.created_at or s.first_seen, reverse=True):
        intel_by[s.source.split("market_", 1)[-1]].append(s)
    for k, ind in tax.industries.items():
        items = intel_by.get(k, [])[:3]
        if items:
            L.append(f"**{ind.name}**")
            L += [f"- [{_cell(s.title, 120)}]({s.url}) ({(s.created_at or s.first_seen)[:10]})" for s in items]
            L.append("")

    L += ["## Source health (last run)", "", "| Source | Signals | Status |", "|---|---|---|"]
    for name, info in run.get("sources", {}).items():
        L.append(f"| {name} | {info.get('count', 0)} | {_cell(info.get('error') or 'ok', 80)} |")
    L += ["", "<details><summary>HTTP stats</summary>", "", "```json",
          json.dumps(run.get("http", {}), indent=1), "```", "</details>", ""]

    out_dir.mkdir(parents=True, exist_ok=True)
    md = "\n".join(L)
    (out_dir / "latest.md").write_text(md)
    return {"ideas": ideas, "board": board, "markdown": md}
