# Hermes-Integrated MCP Prediction-Market Agent

A **local, low-latency, paper-trading-first laboratory** that lets an MCP-capable
LLM agent research and operate a Polymarket-style prediction-market workflow
behind **deterministic risk gates**, a **hash-chained audit trail**, an
**auditable learning loop**, and a **compliance-locked live boundary**.

Reference implementation of **PM-MCP-SRS-001** (`hermes_mcp_prediction_market_srs.md`).

> **Paper-first, by design.** Live execution is *disabled by default* and cannot
> be triggered through raw MCP tool arguments. The live tool accepts only a
> reference to a previously risk-approved intent, and every eligibility,
> jurisdiction, age, geoblock, red-team, and signing-vault gate must pass before
> anything could ever be submitted. The MVP is a fast paper laboratory with agent
> supervision - not an uncontrolled money bot.

---

## Why this is not just an API wrapper

The differentiator is the **local control plane around the agent**: paper/live
parity, deterministic validation, source provenance, replayable audit logs, a
dashboard, a learning loop, and a sober promotion process that prevents moving
from paper to live on hype or a short lucky streak. The LLM never sits on the
millisecond-critical path - market data, order-book state, paper fills, and risk
checks run in a fast in-process daemon; the agent reasons, explains, and improves.

## Architecture (five lanes)

```
            MCP stdio                              in-process (Fast Lane)
 Agent  <------------->  MCP Server  -- calls -->  Core Trading Daemon
 (Hermes/IDE/Codex)      (tools/res/prompts)        |- Market Data Engine -> Polymarket / synthetic / replay
                                                    |- Hot Order-Book Cache (+ staleness)
                                                    |- Risk Engine (deterministic, versioned)
                                                    |- Paper Engine (fills + double-entry ledger)
                                                    |- Signals (X + weather/sports/news)
                                                    |- Learning (postmortems + lessons)
                                                    |- Campaign / Evaluation / Promotion
                                                    |- Live Adapter (LOCKED, reference-only)
                                                    \- Audit Store (append-only, hash-chained)
 Browser <-- WebSocket/REST -- Dashboard (FastAPI) -- shares the daemon --/   + Prometheus /metrics
```

| Lane | Components |
|------|-----------|
| Data | Market data engine, hot cache, synthetic/replay/live sources, X + external signal adapters |
| Execution | Risk engine, paper engine + ledger, locked live adapter + signing vault |
| Control | MCP server (45 tools, 9 resource types, 6 prompts) |
| Intelligence | Trade-intent service, postmortems, compact lessons, Hermes memory bridge |
| Observation | Dashboard, Prometheus metrics, hash-chained audit store, replay engine |

## Tech choice

Implemented as a single cohesive **Python 3.11 asyncio** system. The SRS
*recommends* Rust/Go for the core but explicitly states the file layout/tech is a
recommendation, not a requirement. Python gives one fully-integrated, fully-tested
artifact; the latency NFRs are met and **benchmarked** (see Testing). SQLite (WAL)
is the local persistent store; an in-process event bus + in-memory cache form the
Fast Lane.

---

## Install

Requires Python 3.11+.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -U pip
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
```

(Linux/macOS: use `./.venv/bin/python`.)

## Quickstart

**1) Run a scripted demo campaign and open the dashboard:**

```powershell
.\.venv\Scripts\python.exe -m hermes_pm.cli            # hermes-pm-demo
# -> prints a localhost dashboard URL; Ctrl+C to stop
# add --no-serve to just run the campaign and print verdicts
```

**2) Run the dashboard against a fresh daemon:**

```powershell
.\.venv\Scripts\hermes-pm-dashboard.exe                # http://127.0.0.1:8787
```

**3) Run the MCP server (stdio) for an agent to connect to:**

```powershell
.\.venv\Scripts\hermes-pm-mcp.exe
```

**4) (Opt-in) Run the MCP server over Streamable HTTP (localhost + token):**

```powershell
$env:HPM_MCP_HTTP_ENABLED="true"; $env:HPM_MCP_HTTP_TOKEN="choose-a-token"
.\.venv\Scripts\hermes-pm-mcp-http.exe                 # http://127.0.0.1:8989/mcp
# Origin/Host DNS-rebinding protection + bearer token are enforced.
```

The dashboard has eight views - **Overview, Watchlist, Trades (with a
"why did this happen?" panel), Timeline (searchable), Sources, Risk, Learning,
Promotion** - every money figure is labelled **PAPER**, and stale/locked/emergency
states are shown prominently.

## Connecting an MCP agent (stdio)

Point any MCP-capable host at the `hermes-pm-mcp` command. Example client config
(Claude Desktop / generic MCP host JSON):

```json
{
  "mcpServers": {
    "hermes-pm": {
      "command": "C:\\Users\\User\\Desktop\\polymarketmcp\\.venv\\Scripts\\hermes-pm-mcp.exe",
      "env": { "HPM_MARKET_DATA_SOURCE": "synthetic" }
    }
  }
}
```

A typical agent session (the prompts encode this loop):

1. `start_paper_campaign` -> returns a `dashboard_url` and active limits.
2. `search_markets` / `get_resolution_rules` -> build a watchlist of tradable, clearly-resolvable markets.
3. `gather_evidence` (and again with `counter=true`) -> sourced, sanitized evidence + contradiction check.
4. `propose_trade_intent` (thesis **and** counter-thesis, evidence refs) -> EV + break-even computed.
5. `risk_check_trade_intent` -> deterministic `approve` / `modify` / `reject` with machine-readable reasons.
6. `paper_place_order` -> simulated fills against the live book, double-entry ledger.
7. `generate_postmortem` + `write_lesson` -> learning loop.
8. `get_promotion_report` -> sober verdicts; **never** auto-unlocks live.

## Tool / resource / prompt catalog

- **45 tools** across System, Market Discovery, Market Data, Signals, Campaign,
  Trading Intent, Paper Execution, Live (locked), Learning, Audit.
- **9 resource URI families**: `system://status`, `campaign://{id}/summary`,
  `market://{id}`, `orderbook://{token}`, `portfolio://paper/{id}`,
  `risk://limits/{id}`, `signals://{market}/social`, `lessons://campaign/{id}`,
  `audit://event/{id}`.
- **6 prompts**: `research_market`, `paper_campaign_manager`,
  `trade_intent_reviewer`, `postmortem_closed_trade`, `promotion_report`,
  `live_supervisor`.

All tool inputs are validated against strict JSON Schemas with
`additionalProperties: false` (unknown fields are rejected).

## Configuration

Environment variables use the `HPM_` prefix. Key settings:

| Setting | Default | Notes |
|---|---|---|
| `HPM_MARKET_DATA_SOURCE` | `synthetic` | `synthetic` (offline, deterministic), `replay`, or `live` (Polymarket) |
| `HPM_DASHBOARD_HOST` / `_PORT` | `127.0.0.1` / `8787` | Binds localhost; token required if host != localhost |
| `HPM_DASHBOARD_TOKEN` | `None` | Required for all REST + `/metrics` + `/ws` when non-localhost |
| `HPM_LIVE_ENABLED` | `False` | Insufficient alone - every compliance gate must also pass |
| `HPM_OPERATOR_AGE_VERIFIED` / `_JURISDICTION_ALLOWED` / `_ACKNOWLEDGED_RISK` | `False` | Live eligibility gates |
| `HPM_RED_TEAM_PASSED` | `False` | Prompt-injection red-team sign-off required before live (NFR-SEC-005) |
| `HPM_X_API_ENABLED` / `HPM_X_API_BEARER_TOKEN` | `False` / `None` | Official X API only; offline synthetic otherwise |

Default risk limits (SRS section 14.1): 1% max single-trade risk, 5% per market,
15% per category, 20% correlated (portfolio-wide), 5% daily / 10% campaign loss
stops, >= $200 book depth, <= 0.05 spread, <= 5 s staleness, evidence >= 1 primary
or >= 2 secondary, thesis + counter-thesis required, no leverage/martingale. The
risk policy is **content-addressed** (its `version` is a hash of its fields) and
every decision records the exact version used. Per-campaign `risk_profile`
overrides may **only tighten** limits - they can never loosen a cap or enable a
prohibited behaviour.

## Security & compliance model

- Secrets are **never** returned by any tool, resource, dashboard endpoint, log,
  or audit export (audit exports are redacted by key name).
- All external text (markets, X, news) is **sanitized and tagged untrusted**
  before reaching the model; suspected prompt-injection is flagged and tainted
  evidence is rejected by the risk engine.
- The **signing vault never exposes key material** and is locked; the live adapter
  is reference-only and returns `blocked` until every gate passes.
- The **audit log is append-only and hash-chained**; `verify_chain` detects any
  tampering or reordering. Emergency stop freezes new actions, cancels open paper
  orders, and records an audit event.

## Testing

```powershell
.\.venv\Scripts\python.exe -m pytest -q                  # full suite (211 tests)
.\.venv\Scripts\python.exe -m pytest tests/latency -q    # NFR latency benchmarks
.\.venv\Scripts\python.exe -m ruff check src tests       # static analysis (clean)
```

211 tests across `unit, integration, security, compliance, chaos, replay,
property (Hypothesis fuzz), mutation, latency, e2e` — including contract tests for
the real Polymarket/X clients, a real MCP Streamable-HTTP handshake, encrypted
secret-store, isolated live-process, and hard concurrency/stress simulations.
Coverage highlights:

- **Unit**: risk rules, EV/break-even, order-book math, sanitization, ledger
  zero-sum, discovery, audit chain + tamper detection, intents, evaluation/promotion.
- **Security**: prompt-injection (incl. tab/zero-width/homoglyph/proximity bypass
  attempts), secret-leak probes, MCP schema fuzzing, dashboard auth.
- **Compliance**: live locked by default, all gates, reference-only, geoblock
  fail-closed, emergency stop + audit.
- **Chaos**: process-restart ledger/position recovery, connectivity loss -> stale
  -> risk reject, rate-limit handling, corrupted-snapshot detection.
- **Property**: ledger always balances under random trade sequences; risk engine
  determinism; sanitizer never crashes; economics pessimism invariants.
- **Latency** (measured p95): cached snapshot <= 50 ms, risk check <= 25 ms, paper
  order <= 30 ms, market-data processing <= 10 ms, dashboard push <= 250 ms, X
  processing <= 500 ms.
- **E2E**: one test per acceptance criterion AC-001..AC-008 + full audit-chain
  integrity.

## SRS traceability (summary)

All eight acceptance criteria pass (`tests/e2e/test_acceptance.py`). Coverage:

| Group | Status |
|---|---|
| MCP-SR-001..005 | Implemented (stdio + **Streamable HTTP** with localhost bind, Origin/DNS-rebinding protection, bearer token; strict schemas, no shell, stderr-only on stdio). |
| FR-MD-001..005 | Implemented (Gamma normalize, order-book + resolution eligibility, filters incl. live liquidity/spread). |
| FR-DATA-001..006 | Implemented (WS+REST, hot cache, reconcile/gap detection, staleness, snapshot recording, rate-limit handling). |
| FR-SOC-001..007 | Implemented (official-API-only, provenance, sanitization, multi-dim summary, counter-signal search). |
| FR-EXT-001..005 | Implemented (plug-in adapters + full source metadata; weather/sports fields; stale-vs-horizon reject). |
| FR-TI-001..006 | Implemented (intents not orders, required fields, EV/break-even, thesis+counter, similar-decision recall). |
| FR-RISK-001..007 | Implemented (single deterministic engine, all limits, conservative defaults, machine reasons, versioned policy). |
| FR-PAPER-001..007 | Implemented (limit + marketable fills, partials, pessimistic, double-entry ledger, snapshot provenance). |
| FR-LIVE-001..008 | Implemented as **locked** (disabled default, all gates, geoblock, reference-only, vault isolation, cancel-only, freeze). |
| FR-LEARN-001..006 | Implemented (postmortems, classification, structured + compact lessons, no single-lucky-trade promotion). |
| FR-DASH-001..006 | Implemented (local URL, all panels, searchable timeline, trade-detail "why" panel, emergency + export controls, unsafe-state highlighting). |
| NFR-LAT-001..007 | Met + benchmarked. |
| NFR-REL-001..005 | Met (persist-before-ack, restart recovery, stale on WS loss, idempotency). |
| NFR-SEC-002..006 | Met (no secret leakage, strict schemas, sanitization, red-team gate, dashboard token). || NFR-PRIV-001..004 | Met (local-by-default, retention purge, redacted export). |
| NFR-OBS-001..004 | Met (audit per tool call, traceability, Prometheus metrics incl. throttles/lag/reconnects/push latency). |
| COMP-001..008, PC-001..006, AC-001..008 | Met. |

### Previously-deferred items — now CLOSED with code + tests

These were documented MVP boundaries in the first cut and have since been fully
implemented and tested (they no longer affect any "known scope" caveat):

- **NFR-SEC-001 (secret store)** — `execution/secrets.py` provides an
  env-var store (default), an **encrypted-file store** (Fernet + PBKDF2, verified
  ciphertext-at-rest), and an **OS keychain** store (`keyring`). `SigningVault`
  reads keys only through the store, signs via HMAC, and never returns key
  material. Tests: `tests/security/test_secrets.py`.
- **NFR-SEC-007 (live adapter in a separate process)** — `execution/live_process.py`
  runs the locked adapter in its own OS process with a minimal line-delimited JSON
  IPC surface (references only; secrets live only in the child). Enable with
  `HPM_LIVE_PROCESS_ISOLATION=true`. Tests: `tests/compliance/test_live_process.py`.
- **MCP-SR-002 (Streamable HTTP)** — `mcp/http_server.py` serves the same MCP
  server over Streamable HTTP bound to localhost, with Origin/Host DNS-rebinding
  protection and a required bearer token. Run with `hermes-pm-mcp-http`
  (`HPM_MCP_HTTP_ENABLED=true`, `HPM_MCP_HTTP_TOKEN=...`). Tests:
  `tests/integration/test_mcp_http.py`.

stdio remains the primary/default local transport (MCP-SR-001); HTTP is opt-in.

## Project layout

```
src/hermes_pm/
  config.py models.py errors.py events.py
  util/         hashing, ids, time, sanitize
  persistence/  db (SQLite), redact
  audit/        hash-chained store
  data/         cache, sources (synthetic/replay), polymarket_client, discovery, market_data
  risk/         engine (deterministic)
  execution/    economics, intents, ledger, paper_engine, live_adapter (locked)
  signals/      base, social_x, external, registry
  learning/     postmortem, lessons, hermes_bridge
  campaign/     manager, evaluation, promotion
  metrics/      prometheus registry
  replay/       engine
  daemon/       core (orchestrator)
  mcp/          tools, resources, prompts, server
  dashboard/    server (FastAPI), ui (embedded SPA)
  cli.py
tests/          unit integration security compliance chaos replay property latency e2e
```

## Disclaimer

This software evaluates decisions; it does **not** promise profit. Prediction-market
trading is treated as regulated/gambling-like activity depending on jurisdiction.
The system does not bypass geoblocks, age, jurisdiction, platform terms, or law,
and it ships with live execution disabled.

MIT-licensed. Built to PM-MCP-SRS-001.

