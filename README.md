# signal-collector

An automated routine that searches the internet for **SaaS businesses making more than $1,000/month**.
It focuses on **fast-growing, multi-billion-dollar industries** (on track to grow about 4x in 5 years)
and filters out businesses that conflict with **Islamic values**.

It runs on GitHub Actions every 6 hours and commits its findings to this repo.

👉 **[Read the latest report → `reports/latest.md`](reports/latest.md)**

## What it does

```
 sources ──► fetch ──► extract revenue ──► tag industry ──► halal screen ──► score ──► (Claude review) ──► data/ + reports/
```

| Stage | What happens |
|---|---|
| **Scrape** | Hacker News (Algolia API: stories, comments, and *full expansion* of "what's your revenue?" threads), Reddit (12 subreddits incl. r/SaaS, r/microsaas, r/islamicfinance), dev.to (with full-article fetch for revenue posts), Product Hunt, Medium tag feeds, hnrss, Google News (revenue stories + per-industry market intel). |
| **Extract revenue** | Parses `$4.2k MRR`, `$120k ARR`, `making $3,000 a month`, `€8k/month`, and similar, then converts to USD/month. It ignores numbers that aren't revenue: prices (`$29/mo plan`), funding (`raised $2M`), market sizes, and costs. Each claim gets a confidence score. |
| **Tag industry** | Matches each signal against 11 target industries (`config/industries.yaml`), each with market size, forecast, CAGR, and a source link. |
| **Halal screen** | `config/halal.yaml` **excludes** interest-based lending, gambling, speculative trading, alcohol, drugs, pork, adult content, and deception tools. It **flags for review** debated areas such as music, dating, crypto, and conventional insurance. Excluded items are kept in `data/excluded.jsonl` so you can check the filter. |
| **Score** | 0–100: proven revenue (log-scaled × confidence) 45%, industry growth 30%, community traction 15%, halal-economy fit 10%. |
| **Claude review** *(optional)* | When an `ANTHROPIC_API_KEY` secret is set, Claude reviews the top candidates. It checks that the revenue claim is real and first-person, writes a one-line idea, picks the industry, and gives a halal verdict with a reason. Each post is reviewed only once. |

### How the scrapers handle failures

- Async fetching with a global concurrency limit and a **per-site rate limit**.
- **Retries** use exponential backoff with jitter and respect `Retry-After`.
- **Conditional requests** (ETag / Last-Modified) use a cache that persists between Action runs, so unchanged feeds cost only a quick "not modified" response.
- **Reddit falls back in three steps.** It uses the OAuth API if you add credentials. Otherwise it tries the public JSON. If Reddit blocks that (common for GitHub's servers), it switches every subreddit to RSS for the rest of the run.
- **Duplicates are merged** by normalized URL (tracking parameters removed) and by title fingerprint, so one story syndicated across many news sites counts once.
- **Each source is isolated.** One failing source never stops the run, and every report ends with a source-health table and HTTP stats.

## Target industries

| Industry | CAGR | Why |
|---|---|---|
| AI agents & workflow automation | 46% | $7.8B → $52.6B by 2030 |
| AI in education | 43% | $6.9B → $41B by 2030 |
| Healthcare AI | 39% | $21.7B → $110.6B by 2030 |
| Vertical AI SaaS | 37% | $95B → $1.4T by 2034 |
| Legal AI & compliance | 28% | $3.1B → $10.8B by 2030 |
| AI in agriculture | 25% | $2.8B → $8.5B by 2030 |
| AI cybersecurity | 23% | $31B → $86B by 2030 |
| E-commerce & creator tooling | ~20% | estimate |
| Climate & carbon accounting | 20% | $20B → $41B by 2030 |
| Islamic fintech ☪️ | 18% | $186B → $515B by 2031 |
| Halal economy ☪️ | 7% | $2.43T → $3.36T Muslim consumer spend by 2028 |

The first four are on track for 4x or more in 5 years. The ☪️ verticals are included because they fit Islamic values and are already very large markets. The source for every figure is in `config/industries.yaml`. Analyst forecasts differ, so treat them as directional.

## Data layout

| Path | Contents |
|---|---|
| `reports/latest.md` | Human-readable report (industries, top ideas, emerging signals, items to review, market intel, source health) |
| `reports/archive/YYYY-MM-DD.md` | Daily snapshots |
| `data/ideas.json` | Ranked **qualified** ideas (≥ $1k/month, not excluded) |
| `data/signals.jsonl` | Every tracked signal (one JSON object per line, gives small git diffs) |
| `data/market_intel.jsonl` | Industry growth news |
| `data/excluded.jsonl` | What the halal filter removed, and why |
| `data/industries.json` | Industry leaderboard |
| `data/trends.csv` | Per-run industry counts over time (to see which industries are heating up) |
| `data/runs/` | Per-run stats |

## Setup

1. **Enable Actions write access:** Settings → Actions → General → Workflow permissions → *Read and write*.
2. **Optional secrets** (Settings → Secrets and variables → Actions):
   - `REDDIT_CLIENT_ID` / `REDDIT_CLIENT_SECRET`: create a *script* app at <https://www.reddit.com/prefs/apps>. This is the most reliable way to reach Reddit from GitHub's servers.
   - `ANTHROPIC_API_KEY`: turns on the Claude review pass.
3. **Optional variables:** `CLAUDE_MODEL` (default `claude-opus-5`) and `LLM_MAX_PER_RUN` (default 25).
4. Run it once by hand: Actions → *Collect SaaS signals* → *Run workflow*.

Scheduled workflows only run from the default branch.

## Local use

```bash
pip install -r requirements-dev.txt
python -m signal_collector                        # full run
python -m signal_collector --sources hackernews   # one source
python -m signal_collector --report-only          # rebuild reports from data/
python -m pytest -q
```

## Customising

- **Add a feed:** add any RSS/Atom URL under `rss.feeds` in `config/sources.yaml`.
- **Add an industry:** add a block to `config/industries.yaml` (keywords + market numbers).
- **Adjust the Islamic screen:** edit `config/halal.yaml` (`exclude` removes items, `review` flags them).
