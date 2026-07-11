# Stock Signal Notifier

A lightweight stock monitoring system for US stocks/ETFs. It fetches delayed/end-of-day market data from Massive/Polygon, stores it in SQLite, computes configurable technical scores, and sends Telegram alerts when saved signal scores cross alert thresholds.

The current deployment target is a single Oracle Cloud VM running Ubuntu, Streamlit, SQLite, systemd services, and Caddy as a reverse proxy.

## Current status

| Area | Status |
|---|---|
| Oracle VM deployment | Working |
| SQLite data storage | Working |
| Massive/Polygon grouped daily fetch | Working |
| Massive/Polygon full-market snapshot scan | Working with Stock Starter plan |
| Historical backfill | Working |
| Streamlit dashboard | Working |
| Configurable Signal Builder | Working |
| Telegram notification engine | Working |
| Intraday/full-market scanning | Working as E2-safe filtered scan cycle |
| Canada/TSX support | Later, requires a second provider |

The project supports daily/end-of-day fetches and a filtered full-market snapshot scan cycle. The snapshot cycle is designed to stay safe on the small Oracle E2.1.Micro VM by filtering the full market before scoring.

## Architecture

```text
Massive/Polygon API
        │
        ▼
 stock-notifier fetch-daily / backfill / run-scan-cycle
        │
        ▼
 SQLite database
        │
        ├── Streamlit dashboard
        │
        ├── Signal Builder / scoring engine
        │
        └── Scheduled Telegram alert engine
```

On the Oracle VM:

```text
Caddy :80/:443 ──► Streamlit 127.0.0.1:8501
systemd timer    ──► scheduled fetch/scan cycle
SQLite           ──► SERVER_DB_FILEPATH
```

## Repository layout

```text
stock-notifier/
├── config/
│   └── symbols.txt
├── deploy/
│   ├── Caddyfile
│   ├── stock-notifier-dashboard.service
│   ├── stock-notifier-fetch.service
│   └── stock-notifier-fetch.timer
├── scripts/
│   └── bootstrap_server.sh
├── src/stock_notifier/
│   ├── cli.py
│   ├── config.py
│   ├── dashboard.py
│   ├── db.py
│   ├── ingest.py
│   ├── models.py
│   ├── notifications/
│   ├── providers/
│   ├── scoring/
│   └── symbols.py
├── tests/
├── .env.example
├── pyproject.toml
└── README.md
```

## Configuration

Copy `.env.example` to `.env` and fill the real private values.

Important: `.env` must never be committed or publicly exposed. `.env.example` should contain placeholders only.

Core variables:

```env
MASSIVE_API_KEY=your_massive_api_key_here
DB_PATH=SERVER_DB_FILEPATH
SYMBOLS_PATH=SERVER_SYMBOLS_FILEPATH
MASSIVE_BASE_URL=https://api.massive.com
MASSIVE_REQUESTS_PER_MINUTE=5
MASSIVE_PROFILE_REQUESTS_PER_MINUTE=120
MASSIVE_HTTP_TIMEOUT_SECONDS=30
LOG_LEVEL=INFO
```

Telegram variables:

```env
TELEGRAM_BOT_TOKEN=your_telegram_bot_token_here
TELEGRAM_CHAT_ID=your_telegram_chat_id_here
DASHBOARD_BASE_URL=https://your-dashboard-domain-or-ip
ALERT_DEFAULT_BUY_THRESHOLD=75
ALERT_DEFAULT_SELL_THRESHOLD=40
ALERT_COOLDOWN_HOURS=12
ALERT_DEFAULT_FREQUENCY_AMOUNT=15
ALERT_DEFAULT_FREQUENCY_UNIT=minutes
ALERT_DEFAULT_START_TIME=09:45
ALERT_DEFAULT_TIMEZONE=America/Toronto
ALERT_DEFAULT_MARKET_HOURS_ONLY=true
ALERT_DRY_RUN=true
```

Scan-cycle variables for the paid Massive/Polygon Starter plan:

```env
SCAN_MAX_SYMBOLS=500
SCAN_MIN_PRICE=5
SCAN_MIN_DAY_VOLUME=500000
SCAN_MARKET_HOURS_ONLY=true
SCAN_LOCK_PATH=SERVER_SCAN_LOCK_FILEPATH
```

Dashboard variables:

```env
DASHBOARD_ADDRESS=127.0.0.1
DASHBOARD_PORT=8501
```

For the current Oracle IP deployment, the private server `.env` can use:

```env
DASHBOARD_BASE_URL=http://SERVER_HOST_OR_DOMAIN
```

## Local setup

From Windows PowerShell:

```powershell
cd "PROJECT_ROOT"
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install --upgrade pip
pip install -e .[dev]
copy .env.example .env
```

Edit `.env`, then initialize:

```powershell
stock-notifier init-db
stock-notifier sync-symbols
```

Fetch latest available daily data:

```powershell
stock-notifier fetch-daily
```

Backfill a small subset for MA/ADX testing:

```powershell
stock-notifier backfill --days 320 --symbols AAPL,MSFT,NVDA,SPY,QQQ
```

Run the dashboard locally:

```powershell
streamlit run src\stock_notifier\dashboard.py
```

## CLI commands

Database and data ingestion:

```powershell
stock-notifier init-db
stock-notifier sync-symbols
stock-notifier sync-profiles --limit 100
stock-notifier sync-profiles --limit 100 --requests-per-minute 240
stock-notifier sync-profiles --symbols AAPL,MSFT,NVDA
stock-notifier fetch-daily
stock-notifier fetch-daily --date 2026-07-08
stock-notifier backfill --days 90 --symbols AAPL,MSFT,SPY
```

`sync-profiles` uses `MASSIVE_PROFILE_REQUESTS_PER_MINUTE`, not the conservative `MASSIVE_REQUESTS_PER_MINUTE` used by normal market-data calls. On a paid Massive stocks plan, increasing profile sync to 120–240 requests/minute makes metadata backfills much faster.

Some snapshot symbols, especially preferred/share-class tickers, may not have a Massive ticker-overview profile. `sync-profiles` records those as unavailable placeholders so they are not retried in every batch.

The dashboard **Services** tab provides safer data-ingestion workflows for larger scopes.

Market snapshot ingestion:

- uses Massive's full-market snapshot endpoint;
- stores lightweight fields: last price, change %, previous close, volume, and dollar volume;
- can run for the full stock universe, selected lists, typed tickers, or lists plus typed tickers;
- supports minimum price, volume, dollar-volume, and max-symbol filters;
- is the preferred first step before heavier enrichment jobs.

Historical data ingestion:

- backfills daily bars for the stock universe, selected lists, typed tickers, or lists plus typed tickers;
- lets you choose 1–5 years of history;
- uses one historical aggregate API call per symbol for the selected date range, not one call per day;
- supports chunk size, chunks-to-run, run-all-remaining, and requests/minute controls so large scopes can be populated safely;
- estimates API calls, chunk count, total API time, and selected-run API time before running;
- can automatically continue across multiple chunks after one click;
- shows coverage and run progress in the Services card;
- logs runs as `historical_data` in **Latest runs → Service runs**.

Company profile ingestion:

- choose `Stocks universe`, selected lists, typed tickers, or lists plus typed tickers;
- run only missing profiles or refresh all selected profiles;
- choose chunk size and requests/minute;
- run the next chunk, preserving anything already loaded.

For the full stock universe, market snapshots are fast enough to run frequently. Company profiles are slower because they require one ticker-overview request per symbol; prefer repeated chunks or an overnight server job instead of one huge blocking dashboard run.

Signal scoring:

```powershell
stock-notifier seed-signals
stock-notifier list-signals
stock-notifier score --signal "MA Momentum" --symbols AAPL,MSFT,NVDA
stock-notifier score-all
```

Telegram notifications:

```powershell
stock-notifier telegram-test --dry-run
stock-notifier telegram-test
stock-notifier alert-rules-seed
stock-notifier alerts-scan --dry-run
stock-notifier alerts-scan
stock-notifier alerts-scan --send
stock-notifier alerts-history --limit 20
```

Intraday/full-market scan cycle:

```powershell
stock-notifier run-scan-cycle --dry-run --benchmark --max-symbols 50
stock-notifier run-scan-cycle --benchmark --max-symbols 500
```

The scan cycle performs:

```text
full-market snapshot → price/volume filters → score enabled signals → scheduled alert scan → Telegram
```

## Dashboard

The Streamlit dashboard currently includes:

- **Market data** — Stocks universe/list view selector, searchable symbol/company lookup, range selector, aligned price and volume chart.
- **Lists** — full-width list workspace with existing-list table, create/manage selector, manual ticker management, and signal-derived list creation.
- **Signal Builder** — configurable technical-signal definitions stored in SQLite, with inline component help, examples, and universe selection by lists/tickers.
- **Notifications** — Telegram configuration status, missing-rule prompts for newly created signals, editable scheduled alert rules, pending alerts, recent alerts, and delivery history.
- **Services** — bounded maintenance/data-ingestion actions, including market snapshots, chunked historical-data backfills, and chunked company-profile ingestion for the stock universe, selected lists, or typed tickers.
- **Latest runs** — fetch logs, signal-run history, service-run history, and scan-cycle history.

The Signal Builder supports weighted score components and hard gate/filter components. Current component types include:

- close vs SMA
- close vs EMA
- SMA crossover
- EMA crossover
- ADX
- volume ratio
- latest volume
- dollar volume
- price change %

Starter signal templates:

- `MA Momentum`
- `Institutional Momentum`
- `Volume Breakout`
- `Trend Quality`

### Custom symbol lists

The dashboard has a dedicated **Lists** tab. Use it to create reusable groups such as:

- `Portfolio`
- `Potential`
- `High conviction`
- `AI stocks`
- `Energy watch`
- `Liquid Universe`

The Lists tab is organized as a full-width workspace:

1. **Existing lists** — table of all saved lists, descriptions, counts, and timestamps.
2. **Create/Manage Lists** — dropdown with `Create New List` plus every existing list.
3. **List details** — rename and edit description for the selected list.
4. **Add symbols** — add comma-separated tickers, add the currently selected Market Data chart symbol, or search the stock universe by ticker/company name.
5. **Members** — view current list members and remove individual tickers.
6. **Create or update a list from a signal** — turn a saved signal into a reusable list.
7. **Danger zone** — delete the selected list.

The **Market data** tab lets you choose a single view with checkbox-style selectors:

- **Stocks universe** — the full active stock universe currently in the database.
- Any custom list you created, such as `Portfolio`, `Potential`, or `Liquid Universe`.

Only one Market Data view is active at a time. The table, symbol search, and chart selector all use the selected view.

Signal definitions can then use:

```text
Universe: selected
Selected lists: Portfolio, Potential
Extra selected tickers: NVDA, MSFT
```

The signal will run only on the union of the selected lists and extra tickers. If a scan cycle also passes a filtered symbol set, the signal uses the intersection so VM-safe scan filters still apply.

### Build lists from signals

The Lists tab can create or update a list from any saved signal. This is useful when a signal is mostly a set of gates/filters and you want the passing symbols to become a reusable universe.

Typical use cases:

- `Liquid Universe` — symbols passing minimum dollar-volume or latest-volume gates.
- `Momentum Candidates` — liquid symbols passing momentum/trend gates.
- `Trend Watch` — symbols with close above key moving averages and ADX above a threshold.

Workflow:

1. Create and save a signal in **Signal Builder**.
2. Go to **Lists**.
3. Open **Create or update a list from a signal**.
4. Choose the saved signal.
5. Choose `Create new list` or `Use existing list`.
6. Choose candidate source:
   - `Market snapshot by dollar volume` — faster and safer for broad scans; uses the latest snapshot and scores only the top filtered candidates.
   - `Signal universe` — follows the signal's own selected lists/tickers or all active symbols if no selected universe is configured.
7. Choose filters such as `Eligible only`, minimum score, and max symbols.
8. Choose whether to replace existing list members or append to the list.
9. Click **Build list from signal**.

For a liquidity list, create a signal with a liquidity gate:

```text
Type: Dollar volume
Mode: gate
Op: >=
Threshold: 100000
```

Then use **Lists → Create or update a list from a signal** to build a list such as `Liquid Universe`. Use `Market snapshot by dollar volume` as the candidate source and keep `Eligible only` checked.

For the small Oracle E2.1.Micro VM, prefer this staged workflow:

```text
Market snapshot → liquidity signal/list → backfill liquid list → score/alert liquid list
```

This avoids trying to score or backfill every ticker at once.

## Signal Builder guide

The Signal Builder lets you define a ranking formula without changing Python code. A signal is made of components. Each component calculates one technical value, then uses that value either as a pass/fail filter or as part of the final 0–100 score.

### Score mode vs gate mode

Use **gate** when a condition is mandatory. If a gate fails, the symbol is filtered out and its final score becomes 0. Gates are good for “must be true” rules:

- close must be above SMA50
- SMA50 must be above SMA200
- ADX14 must be at least 25
- volume must be at least 2x average volume

Gate components do not contribute to the final score. In component breakdowns, gate score, weight, and contribution are shown as `0`; only `passed` matters.

Use **score** when a condition should rank symbols but not automatically reject them. Score components are normalized to 0–100, multiplied by their weight, and averaged into the final signal score. Score components are good for “more is better” rules:

- stronger 5-day momentum
- larger distance above SMA20
- higher relative volume
- stronger ADX

Example:

```text
Gate:  Close above SMA50       → must pass
Gate:  ADX14 >= 25             → must pass
Score: 5-day price change %    → ranks stronger movers higher
Score: Volume ratio            → ranks unusual volume higher
```

### Field meanings

| Field | Meaning |
|---|---|
| Type | The indicator/value to calculate. |
| Name | Friendly label used in previews, score breakdowns, and alert explanations. |
| Mode | `gate` filters symbols; `score` contributes to the final ranking. |
| Op | Comparison operator used with threshold. |
| Threshold | The pass line. Example: `>= 0` for “above moving average”; `>= 25` for ADX trend strength; `>= 2` for 2x average volume. |
| Weight | Importance of a score component. Weight 2 counts twice as much as weight 1. Ignored for gates. |
| Period | Number of daily bars used for the indicator. In 15-minute scan cycles, the latest snapshot is appended as the current bar, but historical lookbacks are still daily bars in this version. |
| Slow period | For crossover components only. The main Period is the fast average; Slow period is the slower comparison average. |
| Score min | Raw value that maps near 0 points for a score component. |
| Score max | Raw value that maps near 100 points for a score component. |

### Component types

| Type | Raw value produced | Common use |
|---|---:|---|
| Close vs SMA | Percent distance between latest close and SMA. | Trend gate or price extension score. |
| Close vs EMA | Percent distance between latest close and EMA. | Faster trend gate than SMA. |
| SMA crossover / stack | Percent distance between fast SMA and slow SMA. | MA stack/crossover confirmation. |
| EMA crossover / stack | Percent distance between fast EMA and slow EMA. | Faster crossover confirmation. |
| ADX trend strength | ADX value. | Trend-strength filter or score. |
| Relative volume | Current volume divided by average volume. | Volume breakout detection. |
| Latest volume | Latest volume. In scan cycles this is current day volume from the latest snapshot. | Raw liquidity filter. |
| Dollar volume | Latest close multiplied by latest volume. | Price-adjusted liquidity filter. |
| Price change % | Percent price change over N daily bars. | Momentum ranking. |

### Useful component recipes

Close above SMA50:

```text
Type: Close vs SMA
Mode: gate
Op: >=
Threshold: 0
Period: 50
```

SMA5 above SMA20:

```text
Type: SMA crossover / stack
Mode: gate
Op: >=
Threshold: 0
Period: 5
Slow period: 20
```

SMA50 above SMA200:

```text
Type: SMA crossover / stack
Mode: gate
Op: >=
Threshold: 0
Period: 50
Slow period: 200
```

ADX14 trend strength:

```text
Type: ADX trend strength
Mode: gate
Op: >=
Threshold: 25
Period: 14
```

Volume breakout:

```text
Type: Relative volume
Mode: score
Op: >=
Threshold: 1
Period: 20
Score min: 1
Score max: 3
Weight: 1
```

Dollar-volume liquidity filter:

```text
Type: Dollar volume
Mode: gate
Op: >=
Threshold: 100000
```

This keeps only symbols where:

```text
latest close × latest volume >= 100,000
```

For example, a $10 stock needs at least 10,000 shares of volume to pass.

5-day momentum:

```text
Type: Price change %
Mode: score
Op: >=
Threshold: 0
Period: 5
Score min: 0
Score max: 8
Weight: 1
```

Price-change ranking with dollar-volume filter:

```text
Component 1:
Type: Dollar volume
Mode: gate
Op: >=
Threshold: 100000

Component 2:
Type: Price change %
Mode: score
Op: >=
Threshold: 0
Change days: 1
Score min: 0
Score max: 10
Weight: 1
```

This filters out illiquid symbols, then ranks the remaining symbols by strongest 1-day price change. For a 5-day momentum version, set `Change days` to `5`.

### Example signals

MA Momentum:

```text
Gate:  SMA5 above SMA20
Gate:  Close above SMA50
Score: 5-day momentum, score min 0, score max 8
Score: Close vs SMA20, score min 0, score max 10
```

Institutional Momentum:

```text
Gate:  Close above SMA50
Gate:  SMA50 above SMA200
Gate:  ADX14 >= 25
Score: Close vs SMA50, score min 0, score max 15
```

Volume Breakout:

```text
Gate:  Price change % >= 0 over 1 daily bar
Score: Relative volume, score min 1, score max 3
Score: 5-day momentum, score min 0, score max 8
```

Trend Quality:

```text
Gate:  Close above SMA200
Score: ADX14, score min 15, score max 35
Score: SMA20 above SMA50, score min 0, score max 5
Score: Close vs EMA20, score min 0, score max 8
```

### Practical tips

- Start with gates to remove stocks you do not want, then use scores to rank the survivors.
- Use `threshold = 0` for moving-average distance/crossover checks when you mean “above.”
- Use ADX as trend strength, not direction. Combine it with MA or price-position gates for bullish/bearish context.
- Keep score ranges realistic. If Score max is too high, everything scores low; if too low, everything maxes out at 100.
- For the current E2.1.Micro VM, prefer filtered scan cycles such as `--max-symbols 50` to `500` until you confirm runtime is stable.

## Telegram setup

1. Open Telegram and search for `@BotFather`.
2. Send `/newbot`.
3. Choose a display name, for example `Stock Notifier`.
4. Choose a username ending in `bot`.
5. Copy the bot token into private `.env` as `TELEGRAM_BOT_TOKEN`.
6. Open your new bot chat and send `/start`.
7. In PowerShell, run:

```powershell
$TOKEN = "PASTE_YOUR_BOT_TOKEN_HERE"
$response = Invoke-RestMethod "https://api.telegram.org/bot$TOKEN/getUpdates"
$response.result | ConvertTo-Json -Depth 10
```

8. Use `message.chat.id` as `TELEGRAM_CHAT_ID`.

Do not use the bot ID from `getMe`; Telegram will reject that with:

```text
Forbidden: the bot can't send messages to the bot
```

Test with:

```powershell
stock-notifier telegram-test --dry-run
```

Then set:

```env
ALERT_DRY_RUN=false
```

and run:

```powershell
stock-notifier telegram-test
```

## Alert behavior

Alerts are based on saved `signal_scores`, not unsaved Signal Builder previews.

Default behavior:

- BUY alert fires when score crosses to `>= ALERT_DEFAULT_BUY_THRESHOLD`.
- SELL alert fires when score drops to `<= ALERT_DEFAULT_SELL_THRESHOLD` after a prior higher state.
- Cooldown prevents repeated alerts for the same signal/symbol/direction.
- Dry-run mode records delivery rows without calling Telegram.

Recommended chain:

```text
fetch-daily → score-all → alerts-scan → Telegram
```

## Oracle server operations

SSH key location currently used:

```powershell
$KEY = "KEY_FILEPATH"
$SERVER = "SERVER_HOST"
ssh -i $KEY $SERVER
```

On the server:

```bash
cd SERVER_PROJECT_DIR
source .venv/bin/activate
```

Initialize/update schema:

```bash
stock-notifier init-db
```

Run the daily pipeline manually:

```bash
stock-notifier fetch-daily
stock-notifier score-all
stock-notifier alerts-scan --dry-run
```

Run the 15-minute delayed snapshot scan manually:

```bash
stock-notifier run-scan-cycle --dry-run --benchmark --max-symbols 50
```

Restart dashboard:

```bash
sudo systemctl restart stock-notifier-dashboard.service
systemctl status stock-notifier-dashboard.service --no-pager
```

Check logs:

```bash
journalctl -u stock-notifier-dashboard.service -n 100 --no-pager
journalctl -u stock-notifier-fetch.service -n 100 --no-pager
```

Edit server `.env`:

```bash
nano SERVER_ENV_FILEPATH
```

Lock down server secrets:

```bash
sudo chown ubuntu:ubuntu SERVER_ENV_FILEPATH
chmod 600 SERVER_ENV_FILEPATH
```

## Cloud rollout and backfill procedure

Recommended order:

```text
deploy code → run init-db/migrations → verify dashboard/CLI → ingest/backfill data
```

Do **not** backfill a large database first and then upload code that may require schema changes. Code and schema should go first, because `stock-notifier init-db` creates/upgrades the tables needed by the current version.

Use this default path for normal changes:

1. Deploy local code changes to the Oracle server.
2. SSH into the server.

   ```powershell
   ssh -i "KEY_FILEPATH" SERVER_HOST
   ```

3. Run:

   ```bash
   cd SERVER_PROJECT_DIR
   source .venv/bin/activate
   stock-notifier init-db
   ```

4. Restart the dashboard:

   ```bash
   sudo systemctl restart stock-notifier-dashboard.service
   ```

5. Run lightweight ingestion first:

   ```bash
   stock-notifier run-scan-cycle --dry-run --benchmark --max-symbols 50
   ```

   Or use the dashboard:

   ```text
   Services → Data ingestion: market snapshot
   ```

6. Backfill only the symbols you need for signal testing:

   ```bash
   stock-notifier backfill --days 320 --symbols AAPL,MSFT,NVDA,SPY,QQQ
   stock-notifier score-all
   stock-notifier alerts-scan --dry-run
   ```

7. Expand gradually:

   - lists first, such as `Portfolio` or `Potential`;
   - then filtered/liquid symbols from market snapshots;
   - only later, broad/full-universe historical backfills.

### When to backfill locally vs on the cloud server

Prefer **cloud/server backfill** when:

- the Oracle server is already configured and stable;
- the backfill is modest, such as 50–500 symbols;
- you want the production database to be built directly in `SERVER_DATA_DIR`;
- you want to avoid copying a large SQLite file over SSH.

Prefer **local backfill then upload the SQLite DB** when:

- you are backfilling a large amount of history;
- your local computer is faster and can run for a long time;
- you want to test the database locally before replacing the server DB;
- the Oracle E2.1.Micro is too slow for the initial historical load.

If uploading a local DB, stop the dashboard first so SQLite is not being read while the file is replaced:

```bash
sudo systemctl stop stock-notifier-dashboard.service
cp SERVER_DB_FILEPATH SERVER_DB_FILEPATH.bak
```

From local PowerShell:

```powershell
$KEY = "KEY_FILEPATH"
$SERVER = "SERVER_HOST"
scp -i $KEY "DB_FILEPATH" "${SERVER}:SERVER_DB_FILEPATH"
```

Then on the server:

```bash
cd SERVER_PROJECT_DIR
source .venv/bin/activate
stock-notifier init-db
sudo systemctl start stock-notifier-dashboard.service
```

Only replace the server DB intentionally. The database contains saved signal definitions, lists, scores, alerts, and notification history, so uploading a local DB can overwrite server-created data.

### Practical recommendation right now

For the current project stage:

```text
1. Upload/deploy the latest code first.
2. Run stock-notifier init-db on the server.
3. Use Services → Market snapshot to populate lightweight price/volume data.
4. Use Lists → Create or update a list from a signal to build focused universes such as `Liquid Universe`.
5. Use Services → Historical data to backfill 1–5 years for selected lists or typed tickers in chunks.
6. Run score-all and alerts-scan --dry-run.
7. Add larger historical backfills later, preferably as repeated chunks or an overnight/background service.
```

## Deploy local code changes to Oracle

From local Windows PowerShell:

```powershell
$KEY = "KEY_FILEPATH"
$SERVER = "SERVER_HOST"
$LOCAL = "PROJECT_ROOT"
$REMOTE = "SERVER_PROJECT_DIR"
```

Copy changed source files:

```powershell
scp -i $KEY -r "$LOCAL\src\stock_notifier" "${SERVER}:${REMOTE}/src/"
```

Then on the server:

```bash
cd SERVER_PROJECT_DIR
source .venv/bin/activate
python -m py_compile src/stock_notifier/*.py src/stock_notifier/scoring/*.py src/stock_notifier/notifications/*.py
stock-notifier init-db
sudo systemctl restart stock-notifier-dashboard.service
```

## Security notes

- Never commit `.env`.
- Keep real API keys, Telegram token, and chat ID only in private local/server `.env` files.
- `.env.example` must contain placeholders only.
- If a Telegram bot token is exposed, rotate it with BotFather using `/revoke`.
- Do not open Streamlit port `8501` publicly. Keep it bound to `127.0.0.1`; expose only Caddy on port `80`/`443`.
- On OCI, allow SSH only from your public IP when possible.

## Database tables

Current SQLite schema includes:

- `symbols`
- `symbol_lists`
- `symbol_list_members`
- `company_profiles`
- `daily_bars`
- `market_snapshots`
- `fetch_log`
- `signal_definitions`
- `signal_runs`
- `signal_scores`
- `signal_score_components`
- `notification_channels`
- `alert_rules`
- `alerts`
- `notification_deliveries`
- `alert_state`
- `pending_alerts`
- `service_runs`
- `app_settings`
- `scan_cycle_runs`

`company_profiles` stores Massive ticker overview metadata such as name, primary exchange, market cap, SIC code, and SIC description. Start with SIC description as the first industry classification; richer sector mapping can be added later.

## Testing

Run from local PowerShell:

```powershell
cd "PROJECT_ROOT"
.\.venv\Scripts\Activate.ps1
pytest -q
```

Compile check:

```powershell
python -m py_compile src\stock_notifier\*.py src\stock_notifier\scoring\*.py src\stock_notifier\notifications\*.py
```

Note: invoking Windows Python from WSL can trigger a WSL socket error. Prefer running tests directly from Windows PowerShell.

## Roadmap

| Phase | Deliverable | Status |
|---|---|---|
| 1 | Infra, data fetch, SQLite, dashboard | Complete |
| 2 | Configurable scoring engine and Signal Builder | Complete |
| 3 | Telegram notifications and alert history | Complete |
| 4 | Scheduled alert rules and E2-safe scan cycle | Implemented |
| 5 | Production 15-minute systemd timer and cloud rollout | Next |
| 6 | Backtesting and signal performance analytics | Later |
| 7 | Canada/TSX support via second provider | Later |

## Cost model

Daily/EOD MVP can run at approximately $0/month:

- Oracle Cloud VM: Always Free
- Massive/Polygon: free tier
- Telegram: free
- SQLite: local file

The 15-minute delayed full-market scan cycle requires a paid Massive/Polygon stocks plan such as Stock Starter. Keep `SCAN_MAX_SYMBOLS` conservative on the Oracle E2.1.Micro until benchmark runs prove the VM can keep up.

Likely future cost:

- Massive/Polygon Starter or equivalent for broader/faster US coverage.
- Separate provider for Canadian/TSX coverage.

---

Last updated: 2026-07-10.
