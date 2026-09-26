"""Enrichment + scoring: turns raw signals into ranked, screened ideas."""

from __future__ import annotations

import math

from .classify import Taxonomy
from .extract import best_monthly_revenue
from .models import Signal

QUALIFY_MRR = 1000.0  # the bar: ideas proven to make ≥ $1k/month


def enrich(sig: Signal, tax: Taxonomy) -> Signal:
    blob = f"{sig.title}\n{sig.text}"
    if sig.kind == "idea" and sig.llm is None:
        claim = best_monthly_revenue(blob)
        if claim:
            sig.mrr_usd, sig.revenue_evidence = claim.monthly_usd, claim.snippet
            sig.revenue_confidence = claim.confidence
    if sig.llm is None:
        sig.industries = [k for k, _ in tax.tag(blob)][:3]
        sig.halal_status, sig.halal_reasons = tax.screen(blob)
    return sig


def apply_llm(sig: Signal, tax: Taxonomy) -> Signal:
    """Let a completed LLM review override the heuristic fields."""
    r = sig.llm or {}
    if not r or r.get("error"):
        return sig
    if not r.get("is_revenue_claim"):
        sig.mrr_usd, sig.revenue_confidence = None, 0.0
    elif r.get("mrr_usd"):
        sig.mrr_usd = float(r["mrr_usd"])
        sig.revenue_confidence = 0.9 if r.get("first_person_founder") else 0.7
    ind = r.get("industry")
    if ind in tax.industries:
        sig.industries = [ind] + [k for k in sig.industries if k != ind][:2]
    if sig.halal_status != "excluded" and r.get("halal") in ("ok", "review", "excluded"):
        sig.halal_status = r["halal"]
        sig.halal_reasons = [f"llm: {r.get('halal_reason', '')}"] if r.get("halal_reason") else []
    return sig


def score(sig: Signal, tax: Taxonomy) -> float:
    if sig.halal_status == "excluded" or sig.kind != "idea":
        return 0.0
    conf = sig.revenue_confidence
    rev = min(1.0, math.log10(sig.mrr_usd / 100) / 3) if sig.mrr_usd and sig.mrr_usd > 100 else 0.0
    growth = tax.growth_weight(sig.industries)
    pts = sig.engagement.get("points", 0) + sig.engagement.get("comments", 0)
    eng = min(1.0, math.log1p(pts) / math.log1p(500))
    halal_econ = any(tax.industries[k].halal_economy for k in sig.industries if k in tax.industries)
    s = 0.45 * rev * max(conf, 0.3) + 0.30 * growth + 0.15 * eng + 0.10 * halal_econ
    if sig.llm and sig.llm.get("is_saas") is False:
        s *= 0.5
    s *= 1.0 if sig.halal_status in ("ok", "unknown") else 0.7
    return round(100 * s, 2)


def is_qualified(sig: Signal) -> bool:
    return (sig.kind == "idea" and sig.halal_status != "excluded"
            and (sig.mrr_usd or 0) >= QUALIFY_MRR
            and (not sig.llm or sig.llm.get("is_saas") is not False))
