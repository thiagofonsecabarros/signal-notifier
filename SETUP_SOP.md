# Stock Signal Notifier — Setup and Operating SOP

This runbook takes the project from a laptop checkout to a tested Oracle Cloud deployment.
The current application includes end-of-day ingestion, historical backfill, full-market snapshots,
configurable scoring, Telegram alerts, scheduled scan cycles, and dashboard-managed service jobs.

## 1. Local smoke test

Use Python 3.11 or newer. PowerShell equivalents are shown for Windows.

```powershell
cd "L:\Signal Notifier"
py -3.11 -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements-dev.txt
Copy-Item .env.example .env
# Edit .env and set MASSIVE_API_KEY
stock-notifier init-db
stock-notifier fetch-daily
stock-notifier backfill --days 90 --symbols AAPL,MSFT,SPY
streamlit run src/stock_notifier/dashboard.py
```

Open <http://localhost:8501>. Run the full 51-symbol backfill only when ready: at five
requests/minute it takes about 11 minutes. The grouped daily fetch is one request and is the
normal scheduled job.

Quality checks:

```powershell
pytest
ruff check .
```

## 2. Provision Oracle Cloud

1. In OCI, select a nearby home region with Ampere capacity. Go to **Compute → Instances →
   Create instance**.
2. Choose Ubuntu 24.04 (22.04 is also supported), shape **VM.Standard.A1.Flex**, and keep the
   OCPU/RAM selection inside the Always Free allowance shown in your own console.
3. Use a public subnet and assign a public IPv4 address. Download the generated private SSH key,
   or paste the public half of your existing Ed25519 key. Never upload/share the private key.
4. In the instance subnet's Network Security Group (preferred) or Security List, add TCP ingress:
   port 22 from **your public IP/32**, and ports 80/443 from `0.0.0.0/0`. Do not expose port 8501.
5. Reserve the public IP if OCI created an ephemeral one. Record it as `SERVER_IP`.
6. Connect: `ssh -i /path/to/private_key ubuntu@SERVER_IP`.

OCI rules and UFW are both used. The cloud rule is the outer boundary; UFW protects the host if
the VCN configuration later changes. Confirm a second SSH session works before closing the first.

## 3. Bootstrap and deploy

Copy the repository from the local machine, initially into the Ubuntu user's home:

```bash
scp -i /path/to/private_key -r "L:/Signal Notifier" ubuntu@SERVER_IP:~/stock-notifier
ssh -i /path/to/private_key ubuntu@SERVER_IP
cd ~/stock-notifier
sudo bash scripts/bootstrap_server.sh
sudo rsync -a --delete --exclude .env --exclude .venv ./ /opt/stock-notifier/
sudo chown -R stocknotifier:stocknotifier /opt/stock-notifier
sudo -u stocknotifier python3 -m venv /opt/stock-notifier/.venv
sudo -u stocknotifier /opt/stock-notifier/.venv/bin/pip install --upgrade pip
sudo -u stocknotifier /opt/stock-notifier/.venv/bin/pip install -r /opt/stock-notifier/requirements.txt
sudo -u stocknotifier cp /opt/stock-notifier/.env.example /opt/stock-notifier/.env
sudo -u stocknotifier nano /opt/stock-notifier/.env
sudo chmod 600 /opt/stock-notifier/.env
sudo -u stocknotifier /opt/stock-notifier/.venv/bin/stock-notifier init-db
```

Set `MASSIVE_API_KEY` in `.env`. Keep `DB_PATH=/opt/stock-notifier/data/stock_notifier.db`
and `SYMBOLS_PATH=/opt/stock-notifier/config/symbols.txt` on the server (absolute paths avoid
surprises in services).

## 4. First controlled data run

```bash
sudo -u stocknotifier bash -lc 'cd /opt/stock-notifier && .venv/bin/stock-notifier fetch-daily'
sudo -u stocknotifier bash -lc 'cd /opt/stock-notifier && .venv/bin/stock-notifier backfill --days 90 --symbols AAPL,MSFT,SPY'
sqlite3 /opt/stock-notifier/data/stock_notifier.db \
  'select symbol,trading_date,close,volume from daily_bars order by trading_date desc limit 10;'
sqlite3 /opt/stock-notifier/data/stock_notifier.db \
  'select started_at,run_type,status,bars_written,errors,message from fetch_log order by id desc limit 10;'
```

Expected: the daily command automatically walks backward up to seven calendar days if today is a
weekend/holiday or the current EOD bar is not published yet. A missing watchlist ticker makes the
run `partial`, not fatal. Per-symbol backfill errors are isolated and logged.

## 5. Install services

```bash
sudo cp /opt/stock-notifier/deploy/stock-notifier-*.service /etc/systemd/system/
sudo cp /opt/stock-notifier/deploy/stock-notifier-*.timer /etc/systemd/system/
sudo cp /opt/stock-notifier/deploy/Caddyfile /etc/caddy/Caddyfile
sudo systemctl daemon-reload
sudo systemctl enable --now stock-notifier-dashboard.service
sudo systemctl enable --now stock-notifier-fetch.timer
sudo systemctl restart caddy
systemctl list-timers stock-notifier-fetch.timer
# Enable only after manual dry-run scan cycles are fast and correct:
# sudo systemctl enable --now stock-notifier-scan-cycle.timer
# systemctl list-timers stock-notifier-scan-cycle.timer
# Enable only once service schedules are configured in the dashboard:
# sudo systemctl enable --now stock-notifier-services-scheduler.timer
# systemctl list-timers stock-notifier-services-scheduler.timer
curl -I http://127.0.0.1:8501
curl -I http://SERVER_IP
```

The fetch timer runs weekdays at 22:15 UTC, safely after the US regular close in EST and EDT. `Persistent`
means a missed run is triggered after reboot. The command's date lookback handles market holidays.

The scan-cycle timer runs every 15 minutes on weekdays and calls `stock-notifier run-scan-cycle --benchmark`.
Keep it disabled until `stock-notifier run-scan-cycle --dry-run --benchmark --max-symbols 50 --force` works manually and Latest runs shows acceptable duration. With `SCAN_MARKET_HOURS_ONLY=true`, off-hours timer runs skip instead of fetching/scoring.

The services scheduler timer wakes every 5 minutes and calls `stock-notifier services-run-due`. Configure service schedules in the Services tab and signal schedules in Signal Builder. The scheduler uses the saved service/signal settings and can optionally send Telegram completion/error notifications or compact signal digests. Test manually with `stock-notifier services-run-due --service snapshot --force` or `stock-notifier services-run-due --signal SIGNAL_ID --force`.

Install and enable the services scheduler timer only once, unless the files in `deploy/` change or
the timer was disabled. Creating or editing service/signal schedules in the dashboard updates SQLite settings; it
does not require another `sudo cp`, `daemon-reload`, or `enable --now`.

Scheduled signal runs are designed for 15-minute delayed Massive data. Before scoring, they check whether at least 95% of the signal universe has snapshots fetched in the last 14 minutes. If yes, they reuse the existing snapshot data; otherwise they call Massive's full-market snapshot endpoint once and update only the signal universe. Telegram digests show the top 10 signal results with score, price, percent change, volume, and TradingView/Yahoo links.

The standalone Notifications dashboard page is retired. Configure signal notifications in Signal Builder, service completion/error notifications in Services, and review alert/delivery history in Latest runs.

For HTTPS, point a DNS A record at the reserved IP, replace `:80` in the Caddyfile with the hostname,
and reload with `sudo systemctl reload caddy`. Caddy obtains the certificate automatically.

## 6. Daily operations

```bash
systemctl status stock-notifier-dashboard.service stock-notifier-fetch.timer
systemctl status stock-notifier-services-scheduler.timer --no-pager
journalctl -u stock-notifier-services-scheduler.service -n 100 --no-pager
journalctl -u stock-notifier-fetch.service -n 100 --no-pager
journalctl -u stock-notifier-dashboard.service -n 100 --no-pager
sudo -u stocknotifier sqlite3 /opt/stock-notifier/data/stock_notifier.db \
  'select * from fetch_log order by id desc limit 10;'
```

- A `success` or explainable `partial` entry should appear each US trading day.
- Back up SQLite with its online backup command, not a raw copy while the dashboard is running:
  `sqlite3 data/stock_notifier.db ".backup data/backup-$(date +%F).db"`.
- Before updating, back up the DB and `.env`; deploy code without replacing the server database,
  reinstall requirements if dependencies changed, run `stock-notifier init-db`, then restart the dashboard.
- If a deployment adds a new package folder such as `src/stock_notifier/services`, create the folder
  on the server before copying individual files into it.
- If a deployment changes systemd service/timer files, copy the changed files to `/etc/systemd/system/`
  and run `sudo systemctl daemon-reload`. Normal dashboard schedule edits do not need this.
- Before any deployment that changes SQLite schema, create a server-side DB backup. Use `stock-notifier init-db`
  to add missing tables/columns; do not replace the cloud DB with a local DB unless intentionally restoring
  from backup.
- Rotate the Massive key immediately if it appears in shell history, logs, screenshots, or Git.
- Apply Ubuntu security updates monthly and reboot when `/var/run/reboot-required` exists.

## 7. Troubleshooting gates

| Symptom | Check | Resolution |
|---|---|---|
| HTTP 401/403 | `.env`, plan entitlement | Re-copy the API key; test one grouped endpoint in Massive's console |
| HTTP 429 | `MASSIVE_REQUESTS_PER_MINUTE` | Leave it at 5 on Basic; the client honors Retry-After and backs off |
| No current-day rows | fetch time, holiday | This is normal before EOD publication; inspect the logged actual date |
| Dashboard unavailable externally | Caddy, UFW, NSG/security list | Keep 8501 private; open only 80/443 through both firewall layers |
| Service works in shell only | absolute env paths/ownership | Use `/opt/...` DB and symbols paths; verify `stocknotifier` owns data/ |
| SQLite locked | multiple writers | Keep one scheduled writer; WAL already permits dashboard readers |

## 8. Complete delivery breakdown

1. **Phase 1 — foundation:** local tests, Oracle VM, secrets, SQLite, grouped EOD ingestion,
   historical backfill, dashboard, timer, proxy, logs and backups.
2. **Phase 2 — scoring:** configurable Signal Builder, indicator components, score/gate modes,
   persisted signal definitions, scores, and component breakdowns.
3. **Phase 3 — notifications:** Telegram bot/chat, dry-run mode, alert rules, dedupe/crossing
   state, pending alerts, delivery attempts, alert history, Signal Builder test alerts, and
   Latest runs delivery history.
4. **Phase 4 — delayed intraday:** Massive full-market snapshots, short intraday snapshot history,
   VM-safe filtering, grouped scoring, scan-cycle runs, and 15-minute timer support.
5. **Phase 5 — schedulers:** dashboard-managed market snapshot, price-target ingestion, historical
   backfill, and company profile ingestion; persisted service options; per-signal scheduler controls;
   service/signal-run logging; optional Telegram completion/error notifications and top-10 signal digests.
6. **Phase 6 — price-target context:** import/export existing Investment Analysis target rows,
   show average target in Market Data, and show brokerage targets in Stock details.
7. **Phase 7 — hardening and scale:** production benchmark tuning, larger/liquid universes,
   signal performance analytics/backtesting, richer progress/cancel behavior, and later Canadian
   provider support.

### Phase 1 acceptance checklist

- [ ] Local `pytest` and `ruff check .` pass.
- [ ] No secret is tracked; `.env` mode is 600 on the VM.
- [ ] Grouped fetch and three-symbol backfill succeed.
- [ ] Dashboard shows prices, volume, percent change, charts, and fetch health.
- [ ] Timer survives reboot and produces a fetch-log row.
- [ ] Public traffic reaches Caddy; Streamlit port 8501 is not public.
- [ ] SQLite backup can be restored and queried.
- [ ] Seven trading days complete without an unexplained failed run.
