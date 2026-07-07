# Stock Signal Notifier — Setup and Operating SOP

This runbook takes the project from a laptop checkout to a tested Oracle Cloud deployment.
Phase 1 stores end-of-day US bars. Scoring and Telegram alerts deliberately remain Phase 2/3.

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
sudo cp /opt/stock-notifier/deploy/stock-notifier-fetch.timer /etc/systemd/system/
sudo cp /opt/stock-notifier/deploy/Caddyfile /etc/caddy/Caddyfile
sudo systemctl daemon-reload
sudo systemctl enable --now stock-notifier-dashboard.service
sudo systemctl enable --now stock-notifier-fetch.timer
sudo systemctl restart caddy
systemctl list-timers stock-notifier-fetch.timer
curl -I http://127.0.0.1:8501
curl -I http://SERVER_IP
```

The timer runs weekdays at 22:15 UTC, safely after the US regular close in EST and EDT. `Persistent`
means a missed run is triggered after reboot. The command's date lookback handles market holidays.

For HTTPS, point a DNS A record at the reserved IP, replace `:80` in the Caddyfile with the hostname,
and reload with `sudo systemctl reload caddy`. Caddy obtains the certificate automatically.

## 6. Daily operations

```bash
systemctl status stock-notifier-dashboard.service stock-notifier-fetch.timer
journalctl -u stock-notifier-fetch.service -n 100 --no-pager
journalctl -u stock-notifier-dashboard.service -n 100 --no-pager
sudo -u stocknotifier sqlite3 /opt/stock-notifier/data/stock_notifier.db \
  'select * from fetch_log order by id desc limit 10;'
```

- A `success` or explainable `partial` entry should appear each US trading day.
- Back up SQLite with its online backup command, not a raw copy while the dashboard is running:
  `sqlite3 data/stock_notifier.db ".backup data/backup-$(date +%F).db"`.
- Before updating, back up the DB and `.env`; deploy code with the same `rsync` command, reinstall
  requirements, run `stock-notifier init-db`, then restart the dashboard.
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

1. **Phase 1A — foundation (this scaffold):** local tests, Oracle VM, secrets, SQLite, 51-symbol
   watchlist, grouped EOD ingestion, historical backfill, dashboard, timer, proxy, logs and backups.
2. **Phase 1B — acceptance:** seven consecutive trading-day runs; restore a DB backup; verify reboot
   recovery; add an external uptime check; restrict SSH to your IP and enable OCI account MFA.
3. **Phase 2 — scoring:** add at least 220 adjusted daily bars; implement indicators with explicit
   formulas/tests; version weights and thresholds; persist every score; backtest before treating a
   signal as actionable.
4. **Phase 3 — notifications:** create a Telegram bot/chat; encrypt token in `.env`; edge-trigger
   threshold crossings; add cooldown/deduplication, delivery attempts, alert history, and a daily
   digest. Test with a dry-run notifier first.
5. **Phase 4 — delayed intraday:** upgrade only after Phase 2/3 are stable. Add the full-market
   snapshot provider, exchange-calendar scheduling, 15-minute bars, stale-data guards, and alert
   freshness labels. Do not represent delayed data as real time.
6. **Phase 5 — scale and Canada:** filter active/liquid instruments, benchmark scoring, define
   retention, then add a separately licensed TSX provider. Keep each provider behind the same
   interface and make exchange/date provenance explicit.

### Phase 1 acceptance checklist

- [ ] Local `pytest` and `ruff check .` pass.
- [ ] No secret is tracked; `.env` mode is 600 on the VM.
- [ ] Grouped fetch and three-symbol backfill succeed.
- [ ] Dashboard shows prices, volume, percent change, charts, and fetch health.
- [ ] Timer survives reboot and produces a fetch-log row.
- [ ] Public traffic reaches Caddy; Streamlit port 8501 is not public.
- [ ] SQLite backup can be restored and queried.
- [ ] Seven trading days complete without an unexplained failed run.

