"""End-to-end run with every source served from canned responses (no network)."""

import functools
import json
import time

import httpx

from signal_collector import cli
from signal_collector.http import HttpClient

NOW = int(time.time())

HN_SEARCH = {"hits": [
    {"objectID": "101", "_tags": ["story"], "title": "Show HN: AI receptionist for dental clinics ($6k MRR)",
     "url": "https://dentalbot.io", "author": "a", "points": 240, "num_comments": 95, "created_at_i": NOW - 3600},
    {"objectID": "102", "_tags": ["comment"], "story_id": 200, "story_title": "Ask HN: What's your side project revenue?",
     "comment_text": "Zakat calculator + halal stock screener, we&#x27;re at $2,400 MRR.", "author": "b",
     "created_at_i": NOW - 7200},
    {"objectID": "103", "_tags": ["story"], "title": "Sports betting odds API hits $30k MRR",
     "url": "https://odds.example", "points": 10, "created_at_i": NOW - 100},
]}
HN_ITEM = {"id": 200, "title": "Ask HN: What's your side project revenue?", "children": [
    {"id": 201, "author": "c", "created_at_i": NOW - 50, "text": "Carbon accounting for SMEs, $15k ARR", "children": [
        {"id": 202, "author": "d", "created_at_i": NOW - 40, "text": "nice!", "children": []}]}]}
DEVTO_LIST = [{"id": 7, "title": "How my LMS for tutors reached $3k MRR", "description": "revenue story",
               "url": "https://dev.to/x/lms", "user": {"username": "e"}, "published_timestamp": "2026-09-25T10:00:00Z",
               "public_reactions_count": 40, "comments_count": 5}]
DEVTO_ART = {"id": 7, "body_markdown": "Our tutoring LMS for students now makes $3,000 MRR."}
RSS = """<?xml version="1.0"?><rss><channel>
<item><title>Legal tech: contract review tool at $9k MRR</title><link>https://m.io/p1?utm_source=rss</link>
<description>&lt;p&gt;Solo founder, contract review for law firm clients.&lt;/p&gt;</description>
<pubDate>{date}</pubDate><guid>p1</guid></item></channel></rss>"""
ATOM = """<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom">
<entry><id>t3_abc</id><title>Shopify inventory app doing $1,800 MRR</title>
<link href="https://www.reddit.com/r/SaaS/comments/abc/x/"/><updated>{iso}</updated>
<content type="html">Built for e-commerce sellers.</content><author><name>/u/f</name></author></entry></feed>"""


def handler(request: httpx.Request) -> httpx.Response:
    host, path = request.url.host, request.url.path
    date = time.strftime("%a, %d %b %Y %H:%M:%S +0000", time.gmtime())
    if host == "hn.algolia.com":
        return httpx.Response(200, json=HN_ITEM if path.startswith("/api/v1/items") else HN_SEARCH)
    if host == "www.reddit.com":
        if path.endswith(".json"):
            return httpx.Response(403)  # simulate datacenter block → RSS fallback
        return httpx.Response(200, text=ATOM.format(iso=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())))
    if host == "dev.to":
        return httpx.Response(200, json=DEVTO_ART if path.endswith("/7") else DEVTO_LIST)
    if host == "news.google.com":
        return httpx.Response(200, text=RSS.format(date=date).replace("p1", "g" + str(hash(str(request.url)) % 999)))
    return httpx.Response(200, text=RSS.format(date=date))


def test_full_run(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(cli, "HttpClient", functools.partial(
        HttpClient, transport=httpx.MockTransport(handler), max_retries=0, per_host_interval=0))
    monkeypatch.setattr(cli, "HOST_INTERVALS", {})
    assert cli.main(["--data-dir", "data", "--cache-dir", ".cache"]) == 0

    ideas = json.loads((tmp_path / "data/ideas.json").read_text())
    titles = " ".join(i["title"] + " " + i["text"] for i in ideas)
    assert "AI receptionist" in titles and "Zakat" in titles and "Carbon accounting" in titles
    assert "betting" not in titles  # haram → excluded
    assert all(i["mrr_usd"] >= 1000 for i in ideas)
    # same story from several outlets/feeds collapses to one idea
    assert sum("contract review" in i["title"] for i in ideas) == 1
    excluded = (tmp_path / "data/excluded.jsonl").read_text()
    assert "betting" in excluded
    assert "Shopify inventory" in titles  # reached via reddit RSS fallback
    report = (tmp_path / "reports/latest.md").read_text()
    assert "Top revenue-proven ideas" in report and "AI agents" in report

    # second run: nothing new, first_seen preserved, trends appended
    before = (tmp_path / "data/signals.jsonl").read_text()
    assert cli.main(["--data-dir", "data", "--cache-dir", ".cache"]) == 0
    after = [json.loads(l) for l in (tmp_path / "data/signals.jsonl").read_text().splitlines()]
    assert {r["uid"]: r["first_seen"] for r in after} == \
        {json.loads(l)["uid"]: json.loads(l)["first_seen"] for l in before.splitlines()}
    assert len((tmp_path / "data/trends.csv").read_text().splitlines()) > 12
