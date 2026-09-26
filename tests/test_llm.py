import asyncio
import json
from types import SimpleNamespace

import anthropic

from signal_collector import llm
from signal_collector.classify import Taxonomy
from signal_collector.models import Signal
from signal_collector.pipeline import apply_llm, enrich, is_qualified


class FakeMessages:
    def __init__(self):
        self.calls = []

    async def create(self, **kw):
        self.calls.append(kw)
        out = {"is_saas": True, "idea": "Halal stock screener for retail investors", "target_customer": "Muslim investors",
               "industry": "islamic_fintech", "is_revenue_claim": True, "mrr_usd": 5000,
               "first_person_founder": True, "halal": "ok", "halal_reason": "Screens out riba.",
               "why_promising": "Underserved market"}
        return SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text=json.dumps(out))])


def test_review_applies_verdict(monkeypatch):
    fake = FakeMessages()
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    monkeypatch.setattr(anthropic, "AsyncAnthropic", lambda: SimpleNamespace(beta=SimpleNamespace(messages=fake)))
    tax = Taxonomy()
    s = enrich(Signal("reddit", "1", "My stock screener side project", "https://x.io/s",
                      text="It includes a crypto section. We make $60k ARR."), tax)
    assert s.halal_status == "review"
    assert asyncio.run(llm.review([s], tax)) == 1
    apply_llm(s, tax)
    assert s.mrr_usd == 5000 and s.industries[0] == "islamic_fintech" and s.halal_status == "ok"
    assert is_qualified(s)
    req = fake.calls[0]
    assert req["output_config"]["format"]["type"] == "json_schema" and req["fallbacks"] == "default"
