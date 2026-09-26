"""Optional Claude review of the most promising signals.

Runs only when ``ANTHROPIC_API_KEY`` is set. Each candidate gets a
structured-output verdict: is it really a SaaS business, is the revenue a
genuine first-person claim, which industry, and an Islamic-values assessment.
Verdicts are stored on the signal, so each post is reviewed once.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any

from .classify import Taxonomy
from .models import Signal

log = logging.getLogger(__name__)

DEFAULT_MODEL = "claude-opus-5"

SYSTEM = """You are an analyst screening internet posts for profitable SaaS business ideas \
for a Muslim founder. Judge each post strictly on its content.

Rules:
- is_revenue_claim: true only if the post states a real business's actual revenue/MRR/ARR \
(not a price, a goal, funding, a market size, or a hypothetical).
- mrr_usd: that revenue converted to USD per month (ARR/12), else null.
- first_person_founder: the author claims it about their own business.
- halal: "excluded" if the core business is impermissible in Islam (interest/riba-based \
lending, gambling or betting, alcohol, pork, drugs, adult content, deception/fraud, \
speculative trading signals); "review" if it is debatable (music, dating, conventional \
insurance, crypto, trading tools); "ok" otherwise. halal_reason: one short sentence.
- idea: a one-line description of the SaaS product/idea a founder could build, \
e.g. "AI receptionist for dental clinics". Empty if there is no idea.
- industry: the best-matching key from the list, or "other".
Be concise and never invent numbers."""


def _schema(industry_keys: list[str]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "is_saas": {"type": "boolean"},
            "idea": {"type": "string"},
            "target_customer": {"type": "string"},
            "industry": {"type": "string", "enum": industry_keys + ["other"]},
            "is_revenue_claim": {"type": "boolean"},
            "mrr_usd": {"type": ["number", "null"]},
            "first_person_founder": {"type": "boolean"},
            "halal": {"type": "string", "enum": ["ok", "review", "excluded"]},
            "halal_reason": {"type": "string"},
            "why_promising": {"type": "string"},
        },
        "required": ["is_saas", "idea", "target_customer", "industry", "is_revenue_claim",
                     "mrr_usd", "first_person_founder", "halal", "halal_reason", "why_promising"],
        "additionalProperties": False,
    }


def enabled() -> bool:
    return bool(os.getenv("ANTHROPIC_API_KEY"))


async def review(signals: list[Signal], tax: Taxonomy, max_items: int = 40,
                 concurrency: int = 4) -> int:
    """Review up to ``max_items`` signals in place. Returns how many succeeded."""
    if not signals or not enabled():
        return 0
    import anthropic  # imported lazily: optional dependency

    client = anthropic.AsyncAnthropic()
    model = os.getenv("CLAUDE_MODEL", DEFAULT_MODEL)
    schema = _schema(list(tax.industries))
    industries = "\n".join(f"- {k}: {i.name}" for k, i in tax.industries.items())
    sem = asyncio.Semaphore(concurrency)

    async def one(sig: Signal) -> bool:
        body = f"Industries:\n{industries}\n\nSource: {sig.source}\nTitle: {sig.title}\n" \
               f"URL: {sig.url}\n\nPost:\n{sig.text[:12000]}"
        async with sem:
            try:
                resp = await client.beta.messages.create(
                    model=model,
                    max_tokens=2000,
                    system=SYSTEM,
                    messages=[{"role": "user", "content": body}],
                    output_config={"effort": "low",
                                   "format": {"type": "json_schema", "schema": schema}},
                    betas=["server-side-fallback-2026-07-01"],
                    fallbacks="default",
                )
            except anthropic.RateLimitError as e:
                log.warning("llm rate limited on %s: %s", sig.uid, e)
                return False
            except anthropic.APIStatusError as e:
                log.warning("llm error %s on %s: %s", e.status_code, sig.uid, e.message)
                return False
            except anthropic.APIConnectionError as e:
                log.warning("llm connection error on %s: %s", sig.uid, e)
                return False
        if resp.stop_reason == "refusal":
            sig.llm = {"error": "refusal"}
            return False
        text = next((b.text for b in resp.content if b.type == "text"), "")
        try:
            sig.llm = {**json.loads(text), "model": model}
        except json.JSONDecodeError:
            log.warning("llm returned invalid JSON for %s (stop=%s)", sig.uid, resp.stop_reason)
            return False
        return True

    results = await asyncio.gather(*(one(s) for s in signals[:max_items]))
    return sum(results)
