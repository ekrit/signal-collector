"""Rule-based industry tagging and Islamic-values (halal) screening."""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path
from typing import Any

import yaml

CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"


def _rx(patterns: list[str]) -> re.Pattern[str]:
    return re.compile(r"(?<![\w-])(?:" + "|".join(patterns) + r")(?![\w-])", re.I)


@dataclass
class Industry:
    key: str
    name: str
    cagr: float
    market_usd_bn: float
    market_year: int
    target_usd_bn: float
    target_year: int
    source: str
    keywords: list[str]
    halal_economy: bool = False

    @cached_property
    def pattern(self) -> re.Pattern[str]:
        return _rx([re.escape(k).replace(r"\ ", r"[\s-]?") for k in self.keywords])

    def growth_multiple(self, years: float) -> float:
        return (1 + self.cagr) ** years


class Taxonomy:
    def __init__(self, config_dir: Path = CONFIG_DIR):
        cfg = yaml.safe_load((config_dir / "industries.yaml").read_text())
        self.hyper_multiple = float(cfg.get("hypergrowth_multiple", 4.0))
        self.hyper_years = float(cfg.get("hypergrowth_horizon_years", 5))
        self.industries = {k: Industry(key=k, **v) for k, v in cfg["industries"].items()}

        halal = yaml.safe_load((config_dir / "halal.yaml").read_text())
        self.exclude = {cat: _rx(p) for cat, p in halal["exclude"].items()}
        self.review = _rx(halal["review"])

    # ------------------------------------------------------------- industries
    def tag(self, text: str) -> list[tuple[str, int]]:
        """Industries mentioned in ``text`` with hit counts, strongest first."""
        hits = []
        for key, ind in self.industries.items():
            n = len(ind.pattern.findall(text))
            if n:
                hits.append((key, n))
        return sorted(hits, key=lambda kv: -kv[1])

    def is_hypergrowth(self, key: str) -> bool:
        ind = self.industries.get(key)
        return bool(ind) and ind.growth_multiple(self.hyper_years) >= self.hyper_multiple

    def growth_weight(self, keys: list[str]) -> float:
        """0..1 — how strongly the idea's best industry is growing."""
        best = max((self.industries[k].growth_multiple(self.hyper_years) for k in keys
                    if k in self.industries), default=1.0)
        return min(1.0, (best - 1) / (self.hyper_multiple - 1))

    # ------------------------------------------------------------------ halal
    def screen(self, text: str) -> tuple[str, list[str]]:
        """Return (status, reasons). status ∈ {"ok", "review", "excluded"}."""
        reasons = []
        for cat, rx in self.exclude.items():
            m = rx.search(text)
            if m:
                reasons.append(f"{cat}: '{m.group(0)}'")
        if reasons:
            return "excluded", reasons
        found = sorted({m.group(0).lower() for m in self.review.finditer(text)})
        if found:
            return "review", [f"needs review: {', '.join(found)}"]
        return "ok", []

    def summary(self) -> list[dict[str, Any]]:
        rows = []
        for ind in self.industries.values():
            rows.append({
                "key": ind.key, "name": ind.name, "cagr": ind.cagr,
                "market_usd_bn": ind.market_usd_bn, "market_year": ind.market_year,
                "target_usd_bn": ind.target_usd_bn, "target_year": ind.target_year,
                "growth_5y": round(ind.growth_multiple(self.hyper_years), 2),
                "hypergrowth": self.is_hypergrowth(ind.key),
                "halal_economy": ind.halal_economy, "source": ind.source,
            })
        return sorted(rows, key=lambda r: -r["cagr"])
