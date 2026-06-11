# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.0] - 2026-06-11

Initial release — a reference implementation of **PM-MCP-SRS-001**: a local,
low-latency, paper-trading-first prediction-market laboratory for MCP-capable
agents, with deterministic risk gates, a hash-chained audit trail, an auditable
learning loop, and a compliance-locked live boundary.

### Added

- **MCP server** over stdio (primary) and opt-in Streamable HTTP with localhost
  binding, Origin/Host DNS-rebinding protection, and bearer-token auth.
  45 tools, 9 resource URI families, and 6 prompts; all tool inputs validated
  against strict JSON Schemas (`additionalProperties: false`).
- **Market data engine** — synthetic (deterministic/offline), replay, and live
  (Polymarket) sources; hot order-book cache with staleness tracking; WS↔REST
  reconciliation and gap detection; snapshot recording for replay; rate-limit
  handling.
- **Deterministic, versioned risk engine** — exposure, loss-stop, microstructure
  (depth/spread/staleness), and evidence-quality limits; content-addressed policy
  version recorded on every decision; per-campaign overrides may only tighten.
- **Paper execution engine** — limit + marketable fills with partials and
  pessimistic slippage; double-entry ledger that always balances; snapshot-linked
  fill provenance.
- **Signals** — official X API adapter (no scraping) plus pluggable external
  (weather/sports/news) adapters; full source provenance; sanitization and
  untrusted-tagging of all external text; counter-signal search.
- **Trade-intent service** — intents (not orders) with required thesis +
  counter-thesis, EV and break-even computation, and similar-decision recall.
- **Live adapter (LOCKED)** — disabled by default; reference-only; all compliance
  gates (age, jurisdiction, risk acknowledgement, red-team sign-off, fail-closed
  geoblock, signing key) must pass; cancel-only emergency path; optional
  separate-process isolation.
- **Secret storage** — `env`, `encrypted_file` (Fernet + PBKDF2-HMAC-SHA256), and
  `keyring` (OS keychain) backends behind a `SigningVault` that never exposes key
  material.
- **Learning loop** — postmortems, structured + compact lessons, and a Hermes
  memory bridge; no single-lucky-trade promotion.
- **Dashboard** — local FastAPI SPA with eight views (Overview, Watchlist,
  Trades, Timeline, Sources, Risk, Learning, Promotion); PAPER labelling;
  emergency-stop, pause/resume, and audit-export controls; unsafe-state
  highlighting.
- **Audit store** — append-only, hash-chained event log with tamper/reorder
  detection (`verify_chain`) and redacted export.
- **Observability** — Prometheus `/metrics` (data lag, throttles, reconnects, X
  processing, dashboard push latency).
- **Campaign / evaluation / promotion** services with sober, multi-verdict
  promotion reports that never auto-unlock live.
- **Replay engine** for deterministic re-execution from recorded snapshots.
- **Tests** — 211 passing across unit, integration, security, compliance, chaos,
  replay, property (Hypothesis), mutation, latency (NFR benchmarks), and e2e
  (one test per acceptance criterion AC-001..AC-008).
- **Docs** — `README.md`, `docs/TRACEABILITY.md` (full SRS requirement → code →
  test mapping), `SECURITY.md`, this `RUNBOOK.md`, and `.env.example`.

### Fixed

- Declared `cryptography` as a core runtime dependency. It is imported at startup
  via the live adapter's signing vault (encrypted secret store), but was
  previously undeclared, so a fresh `pip install -e .` could fail at import time.
- Added `keyring` as an optional dependency extra (`.[keyring]`) to match its
  lazy-imported, graceful-degradation OS-keychain backend.

### Changed

- Removed development-only scratch scripts (`_probe_*.py`, `_audit_attack*.py`)
  and their scratch data directories from the project root; their checks are
  covered by the committed test suite.
- Broadened `.gitignore` to exclude scratch scripts and local `.env`/secret files
  while keeping `.env.example` tracked.

### Security

- Paper-first by design: live execution is disabled by default and cannot be
  triggered through raw MCP tool arguments.
- Secrets are never returned by any tool, resource, dashboard endpoint, log, or
  audit export (exports are redacted by key name).
- All external text is sanitized and tagged untrusted before reaching the model;
  tainted evidence is rejected by the risk engine.

[0.1.0]: https://semver.org/spec/v2.0.0.html
