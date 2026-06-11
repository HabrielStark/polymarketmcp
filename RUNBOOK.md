# Operations Runbook — Hermes-PM

Practical procedures for running, observing, and recovering the system. All
commands assume the project root and a Python 3.11+ virtual environment.
Examples use Windows PowerShell paths; on Linux/macOS use `./.venv/bin/...`.

---

## 1. Install

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -U pip
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
```

Optional OS-keychain secret backend:

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[dev,keyring]"
```

Verify the install:

```powershell
.\.venv\Scripts\python.exe -m pytest -q          # expect: 211 passed
.\.venv\Scripts\python.exe -m ruff check src tests
```

---

## 2. Run modes

| Goal | Command | Notes |
|---|---|---|
| Scripted demo + dashboard | `hermes-pm-demo` | Runs a paper campaign, prints a localhost dashboard URL. `--no-serve` runs the campaign then exits; `--port N` overrides the port. Data dir: `./.hermes_pm_demo`. |
| Dashboard only | `hermes-pm-dashboard` | http://127.0.0.1:8787 by default. |
| MCP server (stdio) | `hermes-pm-mcp` | Primary transport for MCP agents. stdout = JSON-RPC only. |
| MCP server (HTTP) | `hermes-pm-mcp-http` | Requires `HPM_MCP_HTTP_ENABLED=true` and `HPM_MCP_HTTP_TOKEN`. Serves `/mcp` on localhost. |

Run the executables from `.\.venv\Scripts\` (e.g. `.\.venv\Scripts\hermes-pm-demo.exe`)
or via `python -m hermes_pm.cli` for the demo.

---

## 3. Configuration

All configuration is via `HPM_`-prefixed environment variables. See
`.env.example` for the complete, annotated list with defaults. Key knobs:

- `HPM_MARKET_DATA_SOURCE` — `synthetic` (default) | `replay` | `live`.
- `HPM_DASHBOARD_HOST` / `HPM_DASHBOARD_PORT` — bind address; token required if
  not localhost (`HPM_DASHBOARD_TOKEN`).
- `HPM_DATA_DIR` — where the SQLite ledger and audit log live.

> The app reads variables from the process environment; it does not auto-load
> `.env`. Export them in your shell or use `dotenv run -- <command>`.

---

## 4. Data, persistence & backup

- State lives in `HPM_DATA_DIR` (default `./.hermes_pm_data`) as a SQLite
  database (`hermes_pm.sqlite3`) in WAL mode: the ledger, positions, intents,
  risk decisions, snapshots, and the hash-chained audit log.
- **Persist-before-ack (NFR-REL-001):** writes are committed before actions are
  acknowledged, so a crash cannot ack an unpersisted trade.
- **Backup:** stop the process (or ensure quiescence), then copy the entire data
  directory, including `*.sqlite3`, `*.sqlite3-wal`, and `*.sqlite3-shm`.
- **Restore:** put the files back into `HPM_DATA_DIR` and restart.

---

## 5. Recovery after restart (NFR-REL-002)

On startup the daemon rebuilds the ledger and open positions from persisted
state. To verify a clean recovery:

1. Start the dashboard or demo against the existing `HPM_DATA_DIR`.
2. Open the dashboard → **Overview**; confirm bankroll, positions, and P&L match
   pre-restart values.
3. Open **Timeline** and run the audit-chain verification (or call the audit
   `verify_chain` tool) — it must report an intact chain.

If the chain reports a break, treat the data dir as compromised: stop, preserve
a copy for investigation, and restore from a known-good backup.

---

## 6. Connectivity loss & stale data (NFR-REL-003)

In `live`/`replay` modes, if the market-data WebSocket drops, the hot cache is
marked **stale** after `HPM_WS_RECONNECT_STALE_MS` (default 5000 ms). The risk
engine **rejects new orders on stale data** (FR-DATA-004). No operator action is
required to stay safe; trading resumes automatically once fresh data returns.
Stale state is highlighted on the dashboard.

---

## 7. Emergency stop

Use when you need to halt all new activity immediately.

- **Dashboard:** the emergency control (Risk / Overview view) freezes new
  actions, cancels open paper orders, and writes an audit event.
- **MCP agent:** call the emergency-stop tool.

After an emergency stop the system is frozen. Review the **Timeline** for the
recorded event, resolve the underlying cause, then resume/pause via the
dashboard controls.

---

## 8. Observability

- **Dashboard views:** Overview, Watchlist, Trades (with a "why did this happen?"
  panel), Timeline (searchable), Sources, Risk, Learning, Promotion.
- **Prometheus metrics:** `GET /metrics` on the dashboard (token-gated when
  remote). Exposes data lag, throttles, reconnects, X processing, and dashboard
  push latency (NFR-OBS-003/004).

---

## 9. Going live (the locked path)

Live trading is intentionally hard to enable and out of scope for normal
operation. It is **not** a config toggle. Before live could ever activate, every
gate in `SECURITY.md` §1 must independently pass: `HPM_LIVE_ENABLED`, all three
operator gates, the red-team sign-off, a successful (fail-closed) geoblock check,
and a signing key provisioned in the secret store. Even then, orders are
reference-only and pass through the full deterministic risk engine. Do not enable
live unless you have completed the red-team review and confirmed legal
eligibility in your jurisdiction.

---

## 10. Troubleshooting

| Symptom | Likely cause | Action |
|---|---|---|
| `ModuleNotFoundError: cryptography` on startup | install skipped core deps | re-run `pip install -e .` (cryptography is a declared core dependency). |
| Dashboard returns 401 / "token required" | host is non-localhost without a token | set `HPM_DASHBOARD_TOKEN`, or bind to `127.0.0.1`. |
| MCP HTTP refuses connection | transport not enabled | set `HPM_MCP_HTTP_ENABLED=true` and `HPM_MCP_HTTP_TOKEN`. |
| Port already in use | another instance / process | change `HPM_DASHBOARD_PORT` / `HPM_MCP_HTTP_PORT`, or stop the other process. |
| Keyring backend unavailable | `keyring` extra not installed or headless | install `.[keyring]`, or use `HPM_SECRET_STORE=encrypted_file`. |
| Orders rejected as "stale" | market-data feed dropped | expected safety behavior; wait for reconnect or check connectivity. |
| Live tool returns `blocked` | one or more compliance gates not passed | expected; see `SECURITY.md` §1. Do not attempt to bypass. |

---

## 11. Health-check checklist

- [ ] `pytest -q` → 211 passed
- [ ] `ruff check src tests` → clean
- [ ] Dashboard loads and shows **PAPER** labels
- [ ] `/metrics` responds
- [ ] Audit `verify_chain` reports an intact chain
- [ ] Live tool reports `blocked` (unless an intentional, fully-gated live setup)
