"""Extract monthly-revenue claims from free text.

Distinguishes revenue claims ("hit $4.2k MRR", "$60k ARR", "making $3,000 a
month") from look-alikes that are *not* revenue: product prices ("$29/mo
plan"), funding ("raised $2M"), market sizes ("$50B market") and costs.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

FX_TO_USD = {"$": 1.0, "usd": 1.0, "€": 1.08, "eur": 1.08, "£": 1.27, "gbp": 1.27}
MULT = {"k": 1e3, "thousand": 1e3, "m": 1e6, "mm": 1e6, "million": 1e6}
PERIOD_TO_MONTHLY = {"day": 30.0, "week": 4.33, "month": 1.0, "year": 1 / 12}

_MONEY = (
    r"(?P<cur>[$€£]|usd\s?|eur\s?|gbp\s?)?"
    r"(?P<num>\d{1,3}(?:[,.]\d{3})+|\d+(?:\.\d+)?)"
    r"\s?(?P<mult>k|m|mm|million|thousand)?\b"
)
_PERIOD = r"(?:\s?(?:/|per|a|an|each|every)\s?(?P<per>mo|mth|month|yr|year|annum|annually|wk|week|day))"
_METRIC = r"(?P<metric>mrr|arr|monthly recurring revenue|annual recurring revenue|in revenue|revenue|profit|net)"

MONEY_RE = re.compile(_MONEY + _PERIOD + r"?\s?" + _METRIC + r"?", re.I)
METRIC_FIRST_RE = re.compile(
    r"(?P<metric>mrr|arr)\s*(?:of|at|is|was|:|=|to|hit|reached|now)?\s*(?:~|about|around|over|almost)?\s*"
    + _MONEY + _PERIOD + "?", re.I)

REVENUE_CONTEXT = re.compile(
    r"\b(mrr|arr|revenue|making|made|earning|earns?|generat\w*|profit\w*|income|hit|reached|"
    r"crossed|passed|surpassed|doing|bringing in|brings in|sales|net|paying customers|bootstrapped)\b", re.I)
NOT_REVENUE_CONTEXT = re.compile(
    r"\b(raised|raise|funding|seed|series [a-d]|valuation|valued|invest\w*|market size|market is|tam|"
    r"cost(s|ing)?|spend(ing)?|spent|salary|budget|price[ds]?|pricing|plan(s)? (start|from)|starts at|"
    r"costs?|fee|charge[sd]?|subscription is|tier|lifetime deal|ltd|burn|loan|debt|grant|bill(ed)?)\b", re.I)
MIN_MONTHLY, MAX_MONTHLY = 100.0, 5_000_000.0

# Sentence-level signals about *whose* revenue it is and whether it is real yet.
GOAL_OR_HYPOTHETICAL = re.compile(
    r"\b(goals?|target(ing)?|aim(ing)?|want(s|ed)? to|hope|hoping|dreams?|plan(s|ning)? to|trying to|"
    r"wish|would|could|imagine|if|scal(e|ing) to|get(ting)? to|reach(ing)?|path to|road to|how to|"
    r"how do|you can|you could|you'll|say you|let's say|until|someday|eventually|"
    r"not|never|n't|expect\w*|build(ing)? an?|launch(ing)? an?|start(ing)? an?|"
    r"can (become|make|earn|be)|become an?)\b", re.I)
THIRD_PARTY = re.compile(
    r"\b(he|she|his|her|him|they|their|mentor|founders who|other founders|this (guy|founder|indie)|"
    r"indie founder|the founder|someone|people|companies|competitors?|résumé|resume|cv|"
    r"posts?|stories)\b", re.I)
FIRST_PERSON = re.compile(r"\b(i|we|my|our|i'm|we're|i've|we've|us)\b", re.I)


def _sentence(text: str, start: int, end: int) -> tuple[str, str]:
    """(sentence text up to the match, full sentence) around a match."""
    a = max(text.rfind(c, 0, start) for c in ".!?\n") + 1
    ends = [i for i in (text.find(c, end) for c in ".!?\n") if i != -1]
    b = min(ends) + 1 if ends else len(text)
    return text[a:start], text[a:b]


@dataclass
class RevenueClaim:
    monthly_usd: float
    snippet: str
    confidence: float  # 0..1


def _to_float(num: str) -> float:
    # "12,500" / "12.500" (thousands) vs "12.5"
    if re.fullmatch(r"\d{1,3}(?:[,.]\d{3})+", num):
        return float(re.sub(r"[,.]", "", num))
    return float(num.replace(",", ""))


def _norm_period(p: str | None) -> str | None:
    if not p:
        return None
    p = p.lower()
    if p.startswith(("mo", "mth")):
        return "month"
    if p.startswith(("yr", "year", "annu")):
        return "year"
    if p.startswith("w"):
        return "week"
    return "day"


def _claim(m: re.Match[str], text: str) -> RevenueClaim | None:
    g = m.groupdict()
    cur = (g.get("cur") or "").strip().lower()
    metric = (g.get("metric") or "").lower()
    period = _norm_period(g.get("per"))
    if not cur and not metric:
        return None  # bare number: not money
    try:
        value = _to_float(g["num"]) * MULT.get((g.get("mult") or "").lower(), 1.0)
    except ValueError:
        return None
    value *= FX_TO_USD.get(cur or "$", 1.0)

    start, end = m.span()
    before = text[max(0, start - 70):start]
    after = text[end:end + 40]
    ctx = before + m.group(0) + after
    conf = 0.0
    if metric in ("mrr", "monthly recurring revenue"):
        period, conf = "month", 0.95
    elif metric in ("arr", "annual recurring revenue"):
        period, conf = "year", 0.9
    elif metric in ("revenue", "in revenue", "profit", "net"):
        conf = 0.75 if period else 0.45
    elif period and REVENUE_CONTEXT.search(before):
        conf = 0.7
    else:
        return None

    # Nearby non-revenue language (funding, prices, costs) lowers confidence,
    # and kills it unless an explicit MRR/ARR metric is attached.
    if NOT_REVENUE_CONTEXT.search(before[-45:] + m.group(0) + after[:20]):
        if metric not in ("mrr", "arr", "monthly recurring revenue", "annual recurring revenue"):
            return None
        conf -= 0.25
    # Is it the author's own, current revenue?
    pre, sent = _sentence(text, start, end)
    if len(pre.strip()) < 15:  # fragment like "$1M ARR." — the lead-in is the previous sentence
        pre = _sentence(text, max(0, start - len(pre) - 1), start)[0] + pre
    if GOAL_OR_HYPOTHETICAL.search(pre) or sent.rstrip().endswith("?"):
        conf -= 0.6          # a goal, a question or a "what if"
    if pre.count('"') % 2 or pre.count("“") > pre.count("”"):
        conf -= 0.5          # quoted: someone else's headline
    if re.search(r"(≈|=)\s*$", text[max(0, start - 4):start]):
        conf -= 0.5          # a calculation/projection, e.g. "1,000 users ≈ $8.4k/mo"
    if re.search(r"\bfrom\s*$", text[max(0, start - 8):start], re.I):
        conf -= 0.3          # "from $2k to $15k": the starting point, not today
    if THIRD_PARTY.search(sent):
        conf -= 0.35
    elif FIRST_PERSON.search(sent):
        conf += 0.05
    else:
        conf -= 0.1
    if period is None:
        period = "year" if re.search(r"\b(in 20\d\d|last year|this year|per year|annual)\b", ctx, re.I) else "month"
        conf -= 0.15
    monthly = value * PERIOD_TO_MONTHLY[period]
    if not (MIN_MONTHLY <= monthly <= MAX_MONTHLY):
        return None
    snippet = re.sub(r"\s+", " ", ctx).strip()
    return RevenueClaim(monthly_usd=round(monthly, 2), snippet=snippet[:220], confidence=round(min(max(conf, 0.05), 1.0), 2))


def extract_revenue(text: str) -> list[RevenueClaim]:
    """All plausible revenue claims in ``text``, most confident first."""
    if not text:
        return []
    claims: list[RevenueClaim] = []
    seen: set[tuple[int, int]] = set()
    for rx in (METRIC_FIRST_RE, MONEY_RE):
        for m in rx.finditer(text):
            if any(a <= m.start() < b for a, b in seen):
                continue
            c = _claim(m, text)
            if c:
                claims.append(c)
                seen.add(m.span())
    claims.sort(key=lambda c: (c.confidence, c.monthly_usd), reverse=True)
    return claims


def best_monthly_revenue(text: str, min_confidence: float = 0.5) -> RevenueClaim | None:
    """The single most credible claim (highest confidence, then largest)."""
    for c in extract_revenue(text):
        if c.confidence >= min_confidence:
            return c
    return None
