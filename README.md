# Yahoo! Auctions GPU / workstation deal watcher

An agent that watches [ヤフオク](https://auctions.yahoo.co.jp/) for **genuinely underpriced** hardware for
GPU rendering (Karma XPU, Octane, Redshift) and alerts you:

- **GPUs**: RTX 3090 / 3090 Ti, 4080 / 4080 SUPER, 4090 (+ 4090 D), 5080, 5090 (+ 5090 D), plus other
  high-VRAM CUDA/OptiX cards: 4070 Ti SUPER, 5070 Ti, TITAN RTX, Quadro RTX 8000, RTX A5000 / A6000,
  RTX 5000 / 6000 Ada, RTX PRO 5000 / 6000 Blackwell
- **PCs and workstations** containing those GPUs, valued as the sum of their parts
- **AMD EPYC** CPUs, CPU + board (+ RAM) bundles, and servers

## How it decides

```
search (newest first)  ─►  rule prefilter  ─►  fetch description + photos + sold comparables
                                                         │
                                         Claude Haiku 5.5 (effort high) triage
                                                         │
                       uncertain, or "interesting" with low confidence
                                                         │
                                         Claude Sonnet 5.5 (effort high) verification
                                                         │
             ┌───────────────────────────┬───────────────┴──────────────┐
      buy-now ≤ ceiling          auction ending < 24h,          auction with > 24h left,
        → alert now               price ≤ ceiling → alert       price ≤ ceiling → WATCHLIST
                                                                         │
                                                      re-checked ~24h before end: alert if
                                                      still ≤ ceiling, else dropped
```

1. **Market price is learned, not guessed.** Every ~20 h the watcher reads recently *sold* (closed)
   auctions for each model, removes parts/junk/laptops/PCs/outliers, and takes the median. EPYC prices
   come from sold listings of the exact CPU model.
2. **Rule prefilter** (free, no LLM): model detection that ignores "RTX4070 (RTX3090同等)" comparisons,
   OptiPlex 5090s and keyword-stuffed titles. It also drops coolers, boxes, water blocks, cables, ジャンク,
   laptop GPUs, reserved (〇〇様専用) listings, shop "custom PC" ads, and anything already priced above
   85% of market.
3. **Haiku triage** reads the *full description and photos*. That's where ヤフオク bait hides: a title
   like "GeForce RTX 5090 グラフィックボード AK08" with "動作できないと思います" deep in the description,
   core-removed boards, vendor-locked EPYCs, and laptop chips. It returns a verdict, its own market
   estimate, red flags, and a **deal ceiling**: the most you should pay all-in for it to still be a clearly
   good deal. The ceiling is always capped at `deal_ratio` (75%) of market.
4. **Sonnet verification** re-examines anything Haiku marks `uncertain`, or `interesting` with confidence
   below 0.75, and gets Haiku's assessment to challenge.
5. **New auctions** (most start at 1円) are judged on whether the *item* is good, then put on the
   watchlist. You're alerted ~24 h before the end only if the price is still under the ceiling.
   Buy-it-now listings that were too expensive keep being tracked, so a price cut into deal range alerts too.

Each listing is analyzed once, so steady-state cost is small. A typical run costs a few cents; the first
run (deeper sweep, up to 80 triages + 12 verifications) is under ~$1.

## Setup

1. **Secrets** (repo → Settings → Secrets and variables → Actions):
   - `ANTHROPIC_API_KEY` (required)
   - Optional extra alert channels (GitHub issues are always on):
     - `NTFY_TOPIC` (+ `NTFY_TOKEN`): push notifications to your phone via [ntfy](https://ntfy.sh). Pick a
       long random topic name and subscribe to it in the ntfy app.
     - `DISCORD_WEBHOOK_URL`
     - `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `ALERT_EMAIL_TO`
2. **Enable Actions**. `.github/workflows/watch.yml` runs every 2 hours and can also be started by hand
   (Actions → watch → Run workflow).
3. **Turn on issue notifications**: each alert is a GitHub issue labelled `deal`. Watch the repo
   ("All activity" or "Custom → Issues") to get them by email or in the GitHub mobile app. Close an
   issue when you're done with it.

State (`state/state.json`) and the human-readable [`WATCHLIST.md`](WATCHLIST.md) are committed back
after every run.

> **Actions minutes:** private repos get 2,000 free minutes/month on GitHub Free. A run takes about
> 3–5 min, so every 2 h uses roughly 1,100–1,800 min/month. If you're on Free and close to the limit,
> change the cron to every 3 h and set `run_interval_hours: 3`.

## Run locally

```bash
pip install -r requirements.txt
export ANTHROPIC_API_KEY=...
python -m watcher --no-llm      # scrape + prefilter only, prints candidates (free)
python -m watcher --dry-run     # full analysis, prints alerts, doesn't notify or save state
python -m watcher               # real run
pytest -q
```

## Tuning (`config.yaml`)

| Setting | Default | Meaning |
|---|---|---|
| `deal.deal_ratio` | 0.75 | "Well below market" = all-in price ≤ 75% of market |
| `schedule.alert_window_hours` | 24 | When to re-check watchlisted auctions before they end |
| `schedule.final_call_hours` | 0 | Set e.g. `3` for a second reminder close to the end |
| `filters.include_junk` | false | Let ジャンク / untested listings through to the LLM |
| `filters.include_laptops` | false | Laptop GPUs are much weaker than desktop cards with the same name |
| `llm.verify_all_alerts` | false | Have Sonnet double-check every alert, not just uncertain ones |
| `llm.max_triage_per_run` | 80 | Cost cap; leftovers are analyzed on the next run |
| `families.*` | | Models to watch: search queries, title regexes, fallback market price |

To watch something new (e.g. Threadripper PRO), add a family with `queries`, `patterns` and either a
`fallback_market_jpy` or `always_llm: true`.

## Limitations

- It reads Yahoo's public pages (the `__NEXT_DATA__` JSON). If Yahoo changes its page structure, runs
  will fail loudly in the Actions log.
- Auction prices usually jump in the final minutes, so a "still cheap 24 h before end" alert means
  *worth bidding on*, not *guaranteed*. Each alert includes the ceiling to use as your max bid.
- The watcher never bids or buys; it only alerts.
