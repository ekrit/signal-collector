import pytest

from signal_collector.classify import Taxonomy
from signal_collector.extract import best_monthly_revenue, extract_revenue
from signal_collector.models import Signal, canonical_url
from signal_collector.pipeline import enrich, is_qualified, score


@pytest.mark.parametrize("text,expected", [
    ("We just hit $4.2k MRR after 8 months", 4200),
    ("Crossed $120k ARR last week!", 10000),
    ("I'm making $3,000 a month from my Shopify app", 3000),
    ("MRR: $12,500", 12500),
    ("Show HN: my side project doing 1.5k MRR", 1500),
    ("$10k/mo profit from a Chrome extension", 10000),
    ("Revenue is £2k per month", 2540),
])
def test_revenue_claims(text, expected):
    c = best_monthly_revenue(text)
    assert c is not None and c.monthly_usd == pytest.approx(expected)


@pytest.mark.parametrize("text", [
    "Our plan is $29/mo for starters",
    "Pricing starts at $49 per month",
    "We raised $2M seed to build this",
    "The market is $50B",
    "It costs $500/month in server bills",
    "we have 2024 customers and 300 users",
])
def test_not_revenue(text):
    assert extract_revenue(text) == []


@pytest.fixture(scope="module")
def tax():
    return Taxonomy()


def test_industry_tagging(tax):
    tags = dict(tax.tag("An AI receptionist voice agent for dental clinics, HIPAA compliant"))
    assert "ai_agents" in tags and "healthcare_ai" in tags
    assert dict(tax.tag("Zakat calculator and halal investing screener"))["islamic_fintech"] >= 2


@pytest.mark.parametrize("text,status", [
    ("Sports betting odds tracker hits $5k MRR", "excluded"),
    ("Payday loan comparison site", "excluded"),
    ("An AI tool for wineries", "excluded"),
    ("Dating app for professionals", "review"),
    ("Invoice automation for plumbers", "ok"),
])
def test_halal_screen(tax, text, status):
    assert tax.screen(text)[0] == status


def test_hypergrowth(tax):
    assert tax.is_hypergrowth("ai_agents")
    assert not tax.is_hypergrowth("halal_economy")


def test_pipeline_scoring(tax):
    good = enrich(Signal("hn", "1", "AI receptionist for clinics at $8k MRR", "https://x.io/a"), tax)
    bad = enrich(Signal("hn", "2", "Casino affiliate site at $8k MRR", "https://x.io/b"), tax)
    good.score, bad.score = score(good, tax), score(bad, tax)
    assert is_qualified(good) and not is_qualified(bad)
    assert good.score > 30 and bad.score == 0


def test_canonical_url_dedupes():
    assert canonical_url("https://www.Example.com/post/?utm_source=x&id=2") == \
        canonical_url("http://example.com/post?id=2")
