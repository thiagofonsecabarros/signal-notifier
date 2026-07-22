# Live Stock Alerting System for US + Canadian Equities: A Complete Build Plan

## TL;DR
- **Build a DIY Python system on Oracle Cloud's Always-Free ARM VM**, pulling the entire US market in a single Massive (formerly Polygon.io) snapshot call (~$29/mo Starter for 15-min delayed data; real-time currently requires the $199/mo Advanced plan), computing indicators with TA-Lib/pandas-ta, and pushing alerts via a free Telegram bot — a realistic delayed-data cost of **~$29–49/month**.
- **Canada (TSX/TSXV) is the hard part**: no cheap provider offers real-time full-exchange intraday snapshots. Use EODHD ($19.99–29.99/mo) for delayed/EOD TSX + TSXV bulk data; accept that Canadian scanning is best done on daily bars refreshed a few times per session rather than true 15-minute real-time.
- **Off-the-shelf platforms (TradingView, Finviz, Trade Ideas) cannot realistically do custom composite scoring across 5,000+ names with push alerts** — TradingView's Ultimate plan caps at 2,000 active alerts (per TradingView support: "Ultimate Plan: 2000 active alerts (1000 price alerts + 1000 technical alerts)"), and Finviz/Trade Ideas are US-only. Use them as a complement, not the core.

## Current implementation status

The Signal Notifier application now has the main US-focused MVP pieces implemented:

- Oracle/Ubuntu deployment with Streamlit behind Caddy.
- SQLite storage for symbols, lists, company profiles, daily bars, latest snapshots, short intraday snapshot history, signals, scores, alerts, notification deliveries, service runs, app settings, and scan-cycle runs.
- Massive reference-ticker sync for active US stock universe metadata, plus full-market snapshot ingestion that refreshes tradable snapshot symbols without letting liquidity filters shrink the master universe.
- Configurable Signal Builder with score components, gate/filter components, preview rankings, saved signal definitions, and signal-derived list creation.
- Market Data view switching between the full stock universe and custom lists; custom list views load only the selected list's symbols.
- Telegram notification engine with dry-run mode, Signal Builder test alerts, scheduled signal/service delivery, pending alerts, delivery history, and full scan-cycle integration.
- E2-safe 15-minute scan-cycle pipeline:

```text
Massive full-market snapshot → latest snapshot + short intraday history → price/volume filters → grouped signal scoring → alert evaluation → Telegram/dry-run → run health
```

- Services tab with persisted options for market snapshot ingestion, historical data backfill, and company profile ingestion; historical/profile services can run one chunk or all remaining chunks automatically.
- Service scheduler controls per service and per signal: enabled flag, frequency amount/unit, start/end time for minute schedules, weekday filters, and optional Telegram completion/error notices.
- `stock-notifier services-run-due` CLI plus systemd timer files that wake every 5 minutes and execute whichever saved services or signals are due.
- Scheduled signal processing uses Massive snapshot freshness caching: reuse recent snapshots within the 15-minute delayed-data window when possible, otherwise fetch the full-market snapshot once and update the signal universe before scoring.
- Scheduled signal Telegram digests include the top 10 scored symbols with type, score, price, percent change, volume, and compact TradingView/Yahoo/dashboard links.
- Latest runs dashboard with Telegram notification history, fetch logs, signal runs, service runs, and scan-cycle runs displayed with human-readable column names and US Eastern timestamps.
- The old standalone Notifications dashboard page has been retired. Notification setup now lives in Signal Builder and Services; notification history lives in Latest runs.

The remaining work is mostly hardening and scale work: production timer tuning, backtesting/performance analytics, rolling indicator/cache optimizations if benchmarks demand them, and adding a Canadian data provider later.

## Key Findings

1. **The single most important architectural decision is using a "full-market snapshot" endpoint instead of per-ticker calls.** Polygon.io's `get_snapshot_all` returns the current minute bar, day aggregate, previous-day bar, and last trade/quote for *every* traded US stock in **one HTTP request**. This collapses a 5,000-request problem into a 1-request problem and makes 15-minute scanning trivial. Per-ticker or credit-metered APIs (Alpha Vantage, Finnhub free, Twelve Data) are the wrong tool for 5,000-symbol scanning.
2. **Massive (formerly Polygon.io) is the best-value US data source for this use case.** Current published tiers put Starter at $29/month with unlimited calls, 15-minute delayed data and five years of history; Developer at $79/month with 15-minute delayed data, ten years of history and trades; and Advanced at $199/month with real-time data. The user explicitly accepts 15-minute delayed data, so Starter is the sweet spot.
3. **No affordable provider gives you real-time, whole-exchange intraday snapshots for the TSX.** EODHD covers TSX (exchange code `.TO`, ~2,951 tickers) and TSXV (`.V`, ~1,595 tickers) with a bulk "EOD-bulk-last-day" endpoint that returns a whole exchange in one call — but that is end-of-day. Real-time TSX from the source (TMX Datalinx) requires per-exchange subscriber fees and is expensive. This is the genuine constraint in the whole project.
4. **Telegram is the clear winner for notification delivery**: free, instant, reliable, trivial API, supports rich formatting and clickable links to your dashboard. SMS/WhatsApp cost real money and add setup friction with no benefit for a personal system.
5. **Oracle Cloud's Always-Free ARM tier is the best "cloud, always-on, $0" host**; GitHub Actions scheduled jobs are excellent for the once-daily EOD batch but explicitly unreliable for strict 15-minute timing (GitHub's own docs: "The schedule event can be delayed during periods of high loads of GitHub Actions workflow runs... If the load is sufficiently high enough, some queued jobs may be dropped").

## Details

### A. Market Data APIs — comparison

The core scanning question is "how do I get a price + volume + enough history for indicators on 5,000+ symbols every 15 minutes?" This splits into two problems: (1) a **snapshot** of current price/volume for all names each cycle, and (2) **history** (enough daily/intraday bars to compute ADX(14), SMA/EMA, volume averages) which you fetch once and then incrementally update.

| Provider | US coverage | Canada (TSX/TSXV) | Real-time vs delayed | Bulk/snapshot for 5k names | Free tier | Paid entry price | Verdict |
|---|---|---|---|---|---|---|---|
| **Polygon.io** (now "Massive") | Excellent, full market | **No Canadian equities** | 15-min delayed (Starter/Developer), real-time (Advanced) | **Yes — `snapshot_all` = 1 call for whole market**; unlimited calls on paid | 5 calls/min, EOD only | $29 Starter / $79 Developer / $199 Advanced | **Best for US** |
| **EODHD** | All US (51k+ tickers) | **Yes — TSX `.TO` & TSXV `.V`** | Delayed live + EOD; real-time WebSocket (50 tickers) | **Yes — `eod-bulk-last-day/{EXCHANGE}` = 1 request/exchange** (EOD) | 20 calls/day | $19.99 EOD-All-World / $29.99 EOD+Intraday | **Best for Canada** |
| **Twelve Data** | All US | Yes (TSX, needs Grow+) | Real-time (Pro+ for WebSocket) | Batch up to 120 symbols/call; credit-metered (1 credit/symbol) | 8 calls/min, 800/day | $29 Grow / $99 Pro / $329 Ultra | Good multi-asset, but credit math is painful at 5k |
| **Finnhub** | US real-time (free) | Limited | Real-time US on free | Per-ticker; no true full-market snapshot | Generous free | ~$49+/mo | US quotes fine; weak for bulk scan |
| **Alpaca** | 100% US via SIP (paid) / IEX (free) | **No Canada** | Free = IEX real-time or 15-min delayed SIP; paid = full SIP | Snapshot endpoints (SIP needs subscription) | Free IEX feed | ~$99/mo Algo Trader Plus (full SIP) | Great if you also trade US via Alpaca |
| **Financial Modeling Prep** | US | Yes (TSX prices API) | Mixed | Bulk endpoints | Limited free | ~$19–99 | Viable backup for Canada |
| **yfinance (Yahoo unofficial)** | US + Canada (append `.TO`) | Yes informally | ~15-min delayed | Batch download, but **fragile** | "Free" | $0 | **Avoid as production dependency** |

**Massive pricing detail (verified 2026-07-04):** Basic is 5 API calls/minute with end-of-day data and two years of history. Paid stock tiers have unlimited API calls. Starter ($29) is 15-minute delayed with five years of history; Developer ($79) remains 15-minute delayed but adds ten years of history and trades; Advanced ($199) is the real-time tier. Check the provider page before purchasing because entitlements change.

**EODHD pricing detail:** Per EODHD's official pricing page — "EOD Historical Data — All World $19.99/mo ($199.00/year = $16.58/mo). EOD+Intraday — All World Extended $29.99/mo ($299.90/year = $24.99/mo)." The Fundamentals feed is $59.99/mo and the All-in-One bundle is $99.99/mo. Paid plans default to 100,000 daily API calls and a 1,000-requests/minute rate limit. Canada is confirmed covered: EODHD sources Canadian fundamentals "with direct extraction from... sedar.com for Canada."

**yfinance caveats (important):** yfinance is an unofficial scraper of Yahoo endpoints with no SLA. Yahoo actively rate-limits and blocks heavy use — a documented GitHub issue (#2128) shows a user who pulled 7,000 tickers daily suddenly hitting HTTP 429 "Too Many Requests." Yahoo's own API terms disclaim all warranties ("THE YAHOO APIS ARE PROVIDED 'AS IS'"). It is fine for prototyping and backfilling history, but building a 5,000-symbol production scanner on it will break. Use it only as a free backfill/secondary source.

**Which tiers cover Canada:** EODHD's paid plans from $19.99/mo cover TSX (`.TO`) and TSXV (`.V`). Twelve Data covers the Toronto Stock Exchange but Canadian/international coverage requires the Grow tier or higher. Polygon and Alpaca do **not** cover Canadian equities at all. Interactive Brokers' API can stream TSX/TSXV data if you hold an IBKR account and subscribe to the Canadian data bundle, billed in CAD (IBKR notes services for TSX depth "only available for Canadian Residents" and Canadian accounts are "billed in Canadian Dollars").

### Practical constraints — doing the rate-limit math

**The 15-minute scan cycle, US side:**
- Universe: ~5,000–11,000 US tickers.
- With Polygon `snapshot_all`: **1 request per cycle.** During a 6.5-hour US session that's 26 cycles → **26 requests/day** for the live snapshot. Trivially within "unlimited calls" on any paid plan.
- History: fetch daily bars (say 60–200 days for ADX/SMA200) once per symbol at onboarding, then append one bar per day — which can be a single grouped-daily aggregate call (whole market, one call).
- **Verdict:** Polygon makes the US side almost embarrassingly easy. The bottleneck is compute (indicators on 5k frames), not API calls.

**The 15-minute scan cycle, Canada side (the real constraint):**
- EODHD `eod-bulk-last-day/TO` and `/V` = **1 request per exchange** (each costs 100 of your 100,000 daily API calls), returns the whole exchange's last-day OHLCV in one shot ("downloading the entire US exchange with more than 45,000 active tickers requires just one API request and 5-10 seconds"). But this is **end-of-day**, not intraday.
- For intraday Canadian prices you'd either (a) poll EODHD's delayed live-quote endpoint per ticker (expensive in calls for 4,500 names), (b) use the 50-ticker WebSocket (only your top watchlist), or (c) accept **daily-bar scanning** for Canada, refreshed once or a few times per session.
- **Recommendation:** Run Canadian scoring on **daily bars** (updated after close via one bulk call), and intraday-poll only a smaller watchlist of high-conviction Canadian names. True real-time 5,000-name TSX scanning is not achievable on a hobby budget.

**Contrast — a per-ticker/credit API at 5,000 names:** Twelve Data charges 1 credit per symbol per call. One full scan = 5,000 credits. The Pro plan gives ~610 credits/minute, so one scan of 5,000 names takes ~8 minutes of continuous calling and consumes your whole quota. This is why credit-metered APIs are unsuitable for whole-universe scanning and snapshot APIs win.

**Market hours handling:** US regular session 09:30–16:00 ET; TSX identical (09:30–16:00 America/Toronto). Gate your scheduler on exchange calendars — both use the same clock but have different holidays (e.g., Canada Day, differing Thanksgiving dates). EODHD and Polygon both expose market-holiday/trading-hours endpoints. Handle pre/post-market separately if desired (both support 4:00–20:00 ET extended hours for US).

### Technical analysis libraries (the DIY compute layer)

You need ADX, SMA/EMA, volume metrics, and % change across thousands of frames.

| Library | Strengths | Weaknesses | Fit |
|---|---|---|---|
| **TA-Lib** (`ta-lib-python`) | C-backed, fastest; Cython/Numpy bindings "2–4× faster than the SWIG interface"; has ADX, ADXR, DX, MACD, RSI, all MAs | Requires compiling the underlying C library (harder install, but fine on a Linux VM) | **Best for speed at 5k symbols** |
| **pandas-ta** | 130+ indicators, pure-Python/Numba, ADX with TradingView-matching `lensig`; auto-uses TA-Lib if installed; `df.ta.strategy()` gives free multiprocessing | Pure-DataFrame path is slower; original project under-maintained (see `pandas-ta-classic` / `pandas-ta-remake` forks) | **Best for ergonomics; pair with TA-Lib backend** |
| **vectorbt** | Vectorized across many symbols at once; excellent for backtesting the scoring rules | Steeper learning curve; overkill for pure live scoring | Use for validating/backtesting your composite score |
| **tulipy** | Fast, lightweight | Fewer indicators, less active | Niche |

**Recommended approach:** Install **TA-Lib** on the VM and drive it through **pandas-ta** (which auto-uses TA-Lib's C implementation for the ~34 core indicators including ADX/EMA/SMA/RSI). Compute on daily bars; for a few thousand symbols this runs in seconds per cycle on the ARM VM. SMA/EMA/%-change are cheap and can also be vectorized with plain NumPy/pandas.

**Composite scoring design (your requirement #3):** Compute per-symbol indicators, then normalize each into a 0–100 sub-score and combine with weights, e.g.:
- Trend strength: ADX(14) > 25 and +DI > −DI → bullish trend points.
- Moving-average stack: price > EMA20 > SMA50 > SMA200 → alignment points.
- Momentum: N-day price change % percentile.
- Volume confirmation: today's volume vs 20-day average volume (e.g., >1.5× = confirmation).

Weighted sum → composite 0–100. Fire **BUY** when the score crosses an upper threshold (e.g., ≥75) *and* it wasn't already above it last cycle (edge-trigger, to avoid repeat alerts); **SELL/exit** when it drops below a lower threshold. Store thresholds in config so you can tune without redeploying.

### Notification delivery — comparison and recommendation

| Channel | Cost | Setup effort | Speed/reliability | Links/rich content | Verdict |
|---|---|---|---|---|---|
| **Telegram Bot API** | **Free** | Low (create bot via @BotFather, get chat_id) | Instant, very reliable | Yes — Markdown, buttons, clickable dashboard links | **Recommended primary** |
| **ntfy.sh** | Free (public) or self-host | Very low (HTTP POST to a topic) | Fast (<1s) | Basic; supports click actions | **Great secondary / self-hosted** |
| **Pushover** | One-time ~$5 per platform | Low | Reliable | Priority levels, good | Solid paid push alternative |
| **Discord webhook** | Free | Very low (paste webhook URL) | Fast | Rich embeds | Good if you live in Discord |
| **Email (Amazon SES)** | 3,000 msgs/mo free for first 12 months, then $0.10/1,000 | Medium (DNS/DKIM, sandbox exit) | Reliable, not instant | Full HTML | Good for daily digests |
| **Email (SendGrid)** | 60-day trial (100/day), then $19.95/mo | Medium | Reliable | Full HTML | SES is cheaper |
| **SMS (Twilio)** | ~$0.0083/segment base + Canadian carrier surcharge (~$0.006–0.009); ~$1.15/mo for a Canadian long-code number | High (number provisioning, opt-out compliance) | Reliable, instant | Text only, no reliable links | Only if you need phone-locked alerts |
| **WhatsApp (Twilio)** | $0.005/msg + Meta template fee ($0.0014–$0.0499) | High (Meta business verification, templates) | Reliable | Rich | Overkill for personal use |

**Twilio Canada specifics (2026):** Twilio's per-segment SMS base rate is **$0.0083 outbound to a Canadian mobile** (identical to the US base rate), plus an automatically-applied Canadian carrier surcharge (roughly $0.0064–$0.0087 depending on carrier — e.g., Bell/Virgin ~$0.0087, Telus ~$0.0073, "all other carriers" ~$0.0064), so an all-in outbound SMS is **~$0.015–$0.017 per segment**. A Twilio-leased Canadian long-code number is **$1.15/month**. Notably, the US-only A2P 10DLC brand/campaign registration does **not** apply to Canada, so Canadian setup is actually simpler than US texting.

**Recommendation:** **Telegram as the primary channel** (free, instant, rich links to alert detail), with **email via Amazon SES for a once-daily digest** of all triggered alerts (well within the free 3,000 messages/month for the first 12 months, then $0.10/1,000). Add **ntfy.sh** as a zero-config backup. Skip SMS/WhatsApp unless you specifically need an alert that rings through on silent — in which case Twilio SMS to a Canadian number is the cheapest paid option, but the base rate roughly doubles once Canadian carrier surcharges are added.

### Architecture options

**Option 1 — Fully DIY Python (RECOMMENDED).**
Scheduler (cron / APScheduler) → data fetch (Polygon snapshot for US + EODHD bulk for Canada) → indicator computation (TA-Lib/pandas-ta) → composite scoring → edge-triggered alert dispatch (Telegram) → write to database → Streamlit/FastAPI dashboard with alert history.
- *Pros:* Full control over the scoring model, cheapest at scale, scales to the entire US+Canada universe, no per-alert limits.
- *Cons:* You build and maintain it; you own reliability.
- *Scales to 5k+:* Yes, easily (snapshot APIs + vectorized indicators).
- *Effort:* MVP in a weekend; polished in 2–4 weekends.

**Option 2 — Hybrid (TradingView/Finviz feeding a custom backend).**
Use TradingView Pine Script alerts or Finviz Elite screener exports as signal sources; TradingView webhooks (require the Essential plan $14.95+/mo and mandatory 2-factor auth; only ports 80/443; 3-second timeout) POST to your FastAPI endpoint, which logs and re-dispatches to Telegram.
- *Pros:* Offloads charting/indicator calc; fast to stand up for a small watchlist.
- *Cons:* **Alert caps kill this at 5k scale.** Per TradingView's support docs: "Premium Plan: 800 active alerts (400 price alerts + 400 technical alerts). Ultimate Plan: 2000 active alerts (1000 price alerts + 1000 technical alerts)." TradingView also raised subscription prices ~17–20% across tiers in April 2026. Watchlist alerts help but still can't cover a custom composite score across 5,000 names with per-symbol thresholds. Finviz Elite is US-only.
- *Scales to 5k+:* No.

**Option 3 — Fully off-the-shelf.**
Finviz Elite ($39.50/mo, or $24.96/mo billed annually) + Trade Ideas ($127–254/mo) + broker scanner.
- *Pros:* Zero code, professional data, real-time (Finviz Elite refreshes quotes in seconds vs. 15–20 min delayed on free).
- *Cons:* No custom composite scoring logic you control, **US-only** (no TSX), expensive, and weak/limited push. Trade Ideas is explicitly "US stocks only. No forex, no crypto, no European markets" — and no Canada.
- *Scales to 5k+:* Screeners scan the universe, but you can't encode your own weighted score or reliably push 5k-name alerts.

**Cost by scenario:**

| Scenario | Data | Host | Notify | DB | Approx. monthly |
|---|---|---|---|---|---|
| **Free/scrappy** | yfinance + EODHD free (20 calls/day) | Oracle Always-Free ARM | Telegram (free) | SQLite on VM | **~$0** (fragile, US daily-bar only) |
| **Budget (recommended MVP)** | Polygon Starter $29 + EODHD EOD $19.99 | Oracle Always-Free ARM | Telegram + SES (free tier) | SQLite / Supabase free | **~$49** |
| **Comfortable/premium** | Massive Advanced $199 (real-time US) + EODHD EOD+Intraday $29.99 | Oracle PAYG or Railway/Hetzner $5–15 | Telegram + SES + Twilio SMS | Neon/Supabase paid or Postgres on VM | **~$235–255** |

### Storage & dashboard

- **History storage:** For a personal system, **SQLite on the VM** is the simplest zero-cost option and handles alert logs and daily snapshots fine. If you want managed Postgres with a nice UI: **Supabase free** (500 MB DB, 1 GB file storage, 5 GB egress — but **pauses after 7 days of inactivity**, so your daily writes keep it alive) or **Neon free** (100 CU-hours/mo, 0.5 GB per project, scale-to-zero). Neon cut storage to $0.35/GB-month and removed its $5 monthly minimum in 2026, so it's cheap to grow into.
- **Dashboard:** **Streamlit** is the fastest way to get a history table + drill-down detail with links (deploy on the same VM). **FastAPI + a small HTML/JS front-end** gives more control and can also receive TradingView webhooks. **Grafana** is great if you want time-series charts of scores over time. A static page regenerated each cycle is the minimal option.

### Hosting — free tiers and paid

| Host | Always-on? | Free tier reality (2026) | Paid entry | Fit for this system |
|---|---|---|---|---|
| **Oracle Cloud Always-Free** | **Yes** | ARM Ampere A1: free-tier accounts now **2 OCPU / 12 GB** (halved from 4/24 as of June 15, 2026); PAYG accounts reportedly retain 4 OCPU/24 GB free; 200 GB storage, 10 TB egress | $0 | **Best — run scheduler + DB + dashboard on one box** |
| **Hetzner** | Yes | None (paid VPS) | ~€4–5/mo | Cheap, reliable, great if you dislike Oracle's console |
| **Railway** | Yes | No permanent free tier; $5 trial credit, then Hobby $5/mo (usage-based) | $5/mo | Easiest DX; fine for the app |
| **Render** | Free web services **spin down after 15 min idle** (30–60s cold start) — bad for a scheduler; static sites free | $7/mo Starter (always-on) | $7/mo | Use paid tier or avoid for cron |
| **Fly.io** | Scale-to-zero possible | No free tier since 2024; ~2-hour trial | ~$2–5/mo | Fine, more "infra" mindset |
| **PythonAnywhere** | Scheduled tasks | Free tier has scheduled tasks (limited) | ~$5/mo | Simple Python cron host |
| **GitHub Actions (cron)** | N/A (scheduled) | Public repos: unlimited free minutes; private repos: 2,000 min/mo on the Free plan | — | **Great for once-daily EOD batch; unreliable for strict 15-min timing** |

**Oracle detail:** Oracle's official docs now state the Always-Free ARM allocation is "the first 1,500 OCPU hours and 9,000 GB hours per month... For Always Free tenancies, this is equivalent to 2 OCPUs and 12 GB of memory." Converting the account to Pay-As-You-Go (a card is held but you're only billed for non-free resources) is the reliable way to avoid "out of capacity" errors and reportedly retains the larger 4 OCPU/24 GB allocation.

**GitHub Actions caveat:** scheduled workflows are explicitly best-effort. GitHub's docs warn: "The schedule event can be delayed during periods of high loads of GitHub Actions workflow runs. High load times include the start of every hour. If the load is sufficiently high enough, some queued jobs may be dropped. To decrease the chance of delay, schedule your workflow to run at a different time of the hour." Use Actions for the daily post-close EOD batch (history append, Canadian bulk pull, daily digest email), not for the 15-minute intraday loop. For the intraday loop, run a long-lived process (APScheduler or a systemd timer) on the Oracle VM. Private-repo Free plans get only 2,000 Actions minutes/month; public repos get unlimited minutes on standard runners.

## Recommendations

**Recommended stack (the concrete answer):**
- **Host:** Oracle Cloud Always-Free ARM VM (Ubuntu) — one box runs the scheduler, database, and dashboard, at $0. Convert to Pay-As-You-Go to dodge capacity limits (still $0 within free limits).
- **US data:** Massive (formerly Polygon.io) — start on **Starter ($29/mo, 15-min delayed)**; upgrade to **Advanced ($199/mo)** only if you later want true real-time. Use the full-market snapshot endpoint.
- **Canada data:** EODHD — **EOD All-World ($19.99/mo)** for TSX + TSXV via the bulk-last-day endpoint; upgrade to **EOD+Intraday ($29.99/mo)** if you want delayed intraday plus the technicals/screener endpoints.
- **Indicators:** TA-Lib (C backend) driven via pandas-ta.
- **Notifications:** Telegram bot (primary) + Amazon SES daily digest (free tier) + ntfy.sh backup.
- **Database:** SQLite on the VM (or Supabase free if you want a hosted UI).
- **Dashboard:** Streamlit on the VM, showing the alert-history table with clickable per-alert detail pages.
- **Total: ~$49/month** (budget scenario), or **~$0** if you start US-only on daily bars with the free tiers before committing.

**Staged implementation plan:**

*Phase 0 — Validate the model (Week 0, $0):* Prototype locally with yfinance/EODHD free tier on ~50 symbols. Build and backtest the composite score with vectorbt to confirm your BUY/SELL thresholds produce sane signals before scaling.

*Phase 1 — US MVP (Week 1, ~$29):* Oracle VM + Polygon Starter. Ingest the full US universe daily (grouped-daily aggregate), compute indicators with TA-Lib/pandas-ta, score, and fire edge-triggered Telegram alerts once daily post-close. Log to SQLite. This alone satisfies requirements 1–5 for US.

*Phase 2 — Intraday loop (Week 2):* Add the 15-minute snapshot loop (Polygon `snapshot_all`) via APScheduler, gated on the US market calendar. Update the last daily bar with the intraday snapshot for near-real-time scoring.

*Phase 3 — Canada (Week 3, +$20):* Add EODHD, daily bulk pull for TSX + TSXV after Toronto close, same scoring pipeline. Intraday-poll only a Canadian watchlist.

*Phase 4 — Dashboard & polish (Week 4):* Streamlit dashboard with alert history + drill-down; SES daily digest email; ntfy backup; a health-check ping so you know the scanner is alive.

**Implemented project phases in this repository:**

1. **Foundation:** local setup, Oracle deployment, SQLite schema, data ingestion, dashboard, Caddy, and basic timers.
2. **Signal Builder:** configurable SQLite-backed signal definitions with weighted score components and gates.
3. **Telegram alerts:** Bot API outbound messages, alert rules, dedupe/crossing state, dry-run mode, delivery history, Signal Builder test alerts, and scheduled signal/service digests.
4. **15-minute scan cycle:** full-market snapshot ingestion, short intraday snapshot history, E2-safe prefilters, grouped scoring, alert evaluation, and scan-cycle health logging.
5. **Services + signal scheduler:** dashboard-managed market snapshot, historical backfill, and company profile jobs with persisted options, service-run logs, systemd wake timer, per-signal scheduled scoring, snapshot reuse, and optional Telegram completion/error notices or top-10 signal digests.

**Next implementation priorities:**

- Keep the production 15-minute scan cycle benchmarked on the current VM before raising `SCAN_MAX_SYMBOLS`.
- Add richer service-run cancellation/progress for long dashboard-triggered jobs if needed.
- Add signal performance analytics/backtesting before relying on scores for portfolio decisions.
- Add Canadian provider support only after the US workflow is stable.

**Benchmarks that would change the plan:**
- If you need **true real-time US** (intraday day-trading rather than swing signals) → upgrade Massive to Advanced ($199); Developer is still 15-minute delayed under the current lineup.
- If **Canadian intraday at scale** becomes essential → budget for TMX Datalinx real-time subscriber fees or an IBKR data bundle (materially more expensive; billed in CAD) — reassess whether it's worth it.
- If the Oracle VM can't keep up with indicator compute for the full universe → move indicators to vectorized NumPy or split the universe across the free OCPUs; only then consider a paid VPS (Hetzner ~€5).
- If you exceed Supabase free limits or hit its 7-day pause → move to Neon or self-hosted Postgres on the VM.
- If Telegram ever proves insufficient (e.g., you miss alerts while asleep) → add Twilio SMS/Pushover for a small set of highest-conviction triggers only.

## Caveats
- **Canadian real-time data is the structural weakness.** Every affordable path (EODHD, Twelve Data, FMP) gives you delayed or end-of-day TSX/TSXV data, not real-time full-exchange intraday. The plan deliberately scores Canada on daily bars. True real-time TSX means TMX Datalinx or IBKR subscriber fees.
- **yfinance is not a production data source.** It's a scraper with no SLA that Yahoo actively throttles (documented HTTP 429 blocks on heavy multi-ticker pulls); use it only for prototyping/backfill.
- **Provider pricing and free tiers change frequently.** Oracle halved its free ARM allocation for free-tier accounts in mid-2026; SendGrid's permanent free tier became a 60-day trial; Railway removed its free tier in 2023; Fly.io removed free allowances in 2024. Verify current pricing at signup.
- **Delayed data means delayed signals.** On the 15-minute Polygon Starter plan and delayed Canadian data, your "live" alerts lag the market by ~15 minutes — fine for swing/position signals, unsuitable for intraday execution.
- **This is a monitoring/alerting system, not trading advice or an execution system.** A composite technical score is a screening heuristic; validate it with backtesting before acting on it, and treat automated signals with appropriate caution.
- **Compliance/ToS:** Respect each provider's terms — especially redistribution clauses. Personal use is fine on EODHD/Polygon personal tiers; sharing or re-displaying the data publicly is not (EODHD's "Internal Use" commercial plan starts at $399/mo). Enable 2FA if you use TradingView webhooks.
