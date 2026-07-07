# Stock Signal Notifier — Project Guide (CLAUDE.md)

> This file is the single source of truth for the project. It gives any AI assistant (or human collaborator) full context: what we're building, the decisions already made, the tech stack, conventions, and the phased roadmap. Keep it updated as the project evolves.

---

## 1. Project Overview

A cloud-hosted stock monitoring and notification system that:

1. **Fetches market data** (15-min delayed is acceptable) for US stocks and ETFs.
2. **Computes technical indicators** per symbol: ADX, SMA/EMA crossovers, volume vs. average volume, price change %, and others to be added.
3. **Scores each asset** with a composite 0–100 score built from those indicators.
4. **Triggers notifications** (Telegram first; email as fallback) when a symbol crosses buy/sell score thresholds.
5. **Keeps an alert history** viewable in a simple web dashboard, with each alert linking to a symbol detail view (chart, indicator values, external links to TradingView/Yahoo Finance).

**Long-term goals (post-MVP):** Canadian (TSX) coverage, 5,000+ symbol universe, smarter scoring, backtesting of scoring rules, multi-channel notifications.

---

## 2. MVP Scope (Current Phase)

| In scope | Out of scope (later phases) |
|---|---|
| US stocks & ETFs only | Canadian/TSX data |
| Polygon.io **free tier** for development/testing (5 calls/min, end-of-day data) | Polygon **Starter** ($29/mo, unlimited calls, 15-min delayed, full-market snapshot) — upgrade once the pipeline works |
| ~50–100 test symbols on the free tier | Full 5,000+ universe (requires Starter's snapshot endpoint) |
| Oracle Cloud Always Free ARM VM as host | Autoscaling, containers/orchestration beyond a single Docker Compose |
| Data fetch + storage pipeline | Scoring engine & notifications (Phase 2–3) |
| Simple read-only frontend showing fetched data | Full alert-history dashboard with detail pages |

**MVP definition of done:** An Oracle Cloud VM runs a scheduled Python job that pulls US stock data from Polygon, stores it in a local database, and a web frontend served from the same VM displays the latest data per symbol.

---

## 3. Architecture

```
┌─────────────────────────── Oracle Cloud Free Tier (Ampere A1 ARM VM) ───────────────────────────┐
│                                                                                                  │
│  scheduler (cron / APScheduler)                                                                  │
│        │                                                                                         │
│        ▼                                                                                         │
│  fetcher (Python) ──► Polygon.io REST API (free tier now → Starter later)                        │
│        │                                                                                         │
│        ▼                                                                                         │
│  SQLite database (MVP) ──► migrate to Postgres later if needed                                   │
│        │                                                                                         │
│        ▼                                                                                         │
│  scorer (Phase 2: pandas-ta / TA-Lib → composite score)                                          │
│        │                                                                                         │
│        ├──► notifier (Phase 3: Telegram Bot API, free)                                           │
│        │                                                                                         │
│        ▼                                                                                         │
│  frontend (FastAPI backend + lightweight web UI, or Streamlit) — port 80/443 via reverse proxy   │
│                                                                                                  │
└──────────────────────────────────────────────────────────────────────────────────────────────────┘
```

---

## 4. Tech Stack & Key Decisions

| Layer | Choice | Rationale |
|---|---|---|
| **Hosting** | Oracle Cloud Always Free — Ampere A1 (up to 4 OCPU / 24 GB RAM), Ubuntu 22.04+ | Genuinely free forever, generous specs, runs 24/7. Note: ARM64 architecture — verify all Python wheels/binaries support ARM. |
| **Market data** | Polygon.io — free tier now, **Starter ($29/mo)** when scaling | Free tier: 5 API calls/min, end-of-day aggregates — fine for building the pipeline against a small symbol list. Starter: unlimited calls, 15-min delayed, **full-market snapshot endpoint** (all US tickers in ~1 call) — the key to scanning 5k+ symbols. US-only (no TSX), which matches MVP scope. |
| **Language** | Python 3.11+ | User preference; rich TA ecosystem. |
| **Scheduler** | cron (simple) or APScheduler (in-process, market-hours aware) | Start with cron; move to APScheduler when market-hours logic matters. |
| **Database** | SQLite (MVP) | Zero setup, one file, fine for single-writer workloads. Migrate to Postgres (local or free Supabase/Neon) if concurrency or size demands it. |
| **TA library (Phase 2)** | pandas-ta (pure Python, easy ARM install) or TA-Lib (faster, C library) | Both compute ADX, SMA/EMA, volume metrics. Start with pandas-ta to avoid ARM build friction. |
| **Notifications (Phase 3)** | Telegram Bot API | Free, unlimited, instant push, trivial HTTP API. Email (SMTP) as fallback. |
| **Frontend** | Option A: **Streamlit** (fastest to build) · Option B: **FastAPI + Jinja2/HTMX or small JS UI** (more control, better long-term) | MVP recommendation: Streamlit for speed; revisit at Phase 4. |
| **Reverse proxy** | Caddy (auto-HTTPS) or Nginx | Expose the frontend on 80/443. |
| **Process management** | systemd services (fetcher, frontend) or Docker Compose | systemd is simplest on a single VM. |
| **Secrets** | `.env` file (never committed) loaded via python-dotenv | Polygon API key, Telegram token later. |

---

## 5. Repository Layout (planned)

```
stock-notifier/
├── README.md            # this file
├── .env.example         # POLYGON_API_KEY=..., DB_PATH=...
├── requirements.txt
├── config/
│   └── symbols.txt      # MVP watchlist (~50–100 US tickers)
├── src/
│   ├── fetcher/         # Polygon client, rate limiting, retries
│   ├── db/              # schema, migrations, data access
│   ├── scorer/          # Phase 2: indicators + composite score
│   ├── notifier/        # Phase 3: Telegram/email dispatch
│   └── frontend/        # dashboard app
├── scripts/             # one-off ops scripts (backfill, healthcheck)
└── deploy/              # systemd unit files, Caddyfile, setup notes
```

---

## 6. Database Schema (MVP)

- **symbols** — ticker, name, exchange, type (stock/ETF), active flag
- **daily_bars** — symbol, date, open, high, low, close, volume (from Polygon aggregates)
- **fetch_log** — run timestamp, symbols fetched, errors, duration (observability from day one)
- *(Phase 2+)* **scores** — symbol, timestamp, indicator values, composite score
- *(Phase 3+)* **alerts** — symbol, timestamp, direction (buy/sell), score, notified channels

---

## 7. Polygon Free-Tier Constraints (important for MVP design)

- **5 API calls/minute** — the fetcher MUST rate-limit itself (e.g., token bucket, 12s between calls) and handle HTTP 429 gracefully with backoff.
- Free tier provides **end-of-day / previous-day data**, not intraday — design the pipeline so switching to Starter's snapshot + 15-min-delayed data later is a config change, not a rewrite. Abstract the data source behind a `MarketDataProvider` interface.
- Use the **grouped daily bars endpoint** (`/v2/aggs/grouped/...`) where possible — it returns the entire US market's daily OHLCV in a single call, which is the most rate-limit-efficient way to populate the DB even on the free tier.
- Backfill history (needed later for ADX/MA which require 20–50+ bars) via per-symbol aggregate calls, throttled.

---

## 8. Roadmap

| Phase | Deliverable | Status |
|---|---|---|
| **1. Infra + Data + Display (MVP)** | Oracle VM provisioned; fetcher pulling Polygon data on schedule into SQLite; frontend showing latest data | ⬅ **current** |
| **2. Scoring engine** | Indicator computation (ADX, SMA/EMA, volume ratio, % change), composite score, score table in DB & frontend | pending |
| **3. Notifications + alert history** | Telegram bot alerts on threshold crossings; alerts table; history page with detail links | pending |
| **4. Scale-up** | Upgrade to Polygon Starter ($29/mo); full US universe via snapshot endpoint; 15-min scan cycle during market hours; frontend hardening | pending |
| **5. Extensions** | TSX/Canada data (requires a second provider — e.g., EODHD or Twelve Data), backtesting of scoring rules, more channels | pending |

**Cost forecast:** $0/mo during MVP (Oracle free tier + Polygon free tier + Telegram free). ~$29/mo at Phase 4 (Polygon Starter). Canadian data at Phase 5 adds ~$20–80/mo depending on provider.

---

## 9. Conventions for AI-Assisted Development

- Python 3.11+, type hints everywhere, `ruff` for lint/format.
- All external calls (Polygon, Telegram) wrapped with retries + timeouts; never let one symbol's failure kill a run.
- All configuration via environment variables / `.env`; no secrets in code or git.
- Every scheduled run writes to `fetch_log` — the system must be debuggable from the DB alone.
- Keep the data-provider layer abstract so Polygon free → Starter → (later) a TSX provider are drop-in changes.
- Small, reviewable commits per component (fetcher, db, frontend separately).

---
---

## 🚀 Initial Prompt — Phase 1, Step 1

Copy the prompt below into a fresh session (e.g., Claude Code on the VM or locally) to kick off implementation:

---

> **Context:** Read `README.md` in this repo — it contains the full project plan, stack decisions, and conventions. We are starting **Phase 1 (MVP)**: Oracle Cloud infra + Polygon data fetcher + simple frontend, US stocks only, Polygon free tier (5 calls/min, end-of-day data).
>
> **Your task — Step 1 of Phase 1, in this order:**
>
> **1. Oracle Cloud VM setup (guide me interactively):**
> - Walk me through provisioning an **Always Free Ampere A1** instance (Ubuntu 22.04 or 24.04, 2 OCPU / 12 GB RAM is plenty to start — leaving free-tier headroom), including SSH key setup, and opening ingress ports 22, 80, and 443 in the VCN security list.
> - Then give me a server bootstrap checklist/script: create a non-root user, enable ufw (or use OCI security lists only — recommend one), install Python 3.11+, pip, git, and set up an app directory at `/opt/stock-notifier`.
>
> **2. Project scaffolding:**
> - Create the repository structure exactly as specified in README section 5, with `requirements.txt`, `.env.example` (POLYGON_API_KEY, DB_PATH), and a `config/symbols.txt` seeded with ~50 liquid US tickers/ETFs (e.g., AAPL, MSFT, NVDA, SPY, QQQ...).
>
> **3. Polygon fetcher (free-tier aware):**
> - Implement `src/fetcher/` with: a rate limiter respecting **5 calls/min**, retry with exponential backoff on 429/5xx, and two fetch modes: (a) **grouped daily bars** (whole-market in one call) filtered to our watchlist, and (b) per-symbol historical backfill (last 60 trading days) for future indicator needs.
> - Implement `src/db/` with the SQLite schema from README section 6 (symbols, daily_bars, fetch_log) and idempotent upserts.
> - Wire a cron-ready entrypoint script that runs the daily fetch and logs to `fetch_log`.
>
> **4. Minimal frontend:**
> - Build a **Streamlit** app in `src/frontend/` that reads the SQLite DB and shows: a table of all watchlist symbols with latest close, volume, and daily % change (sortable), a per-symbol page with a price/volume chart from stored bars, and the last 10 entries of `fetch_log` as a health indicator.
> - Provide the systemd unit files (fetcher timer/service + Streamlit service) and a Caddyfile so the frontend is reachable on port 80.
>
> **Constraints:** Follow all conventions in README section 9. The VM is **ARM64** — verify every dependency installs cleanly on aarch64. Design the data-provider layer so upgrading to Polygon Starter (snapshot endpoint, 15-min delayed) later is a configuration change. Do not build the scoring engine or notifications yet — that's Phase 2–3.
>
> Start with item 1 and wait for my confirmation that the VM is up before generating the code for items 2–4.

---

*Last updated: 2026-07-04 · Phase 1 in progress*
