# Security & Compliance Model

Hermes-PM is a **paper-trading-first** laboratory. Its security posture is built
around four invariants: (1) **live execution is locked by default and cannot be
triggered through raw tool arguments**, (2) **secrets never leave the process**,
(3) **all external text is treated as untrusted**, and (4) **every action is
recorded in an append-only, hash-chained audit log**.

This document describes the model as implemented. Requirement IDs refer to
`hermes_mcp_prediction_market_srs.md`; see `docs/TRACEABILITY.md` for the full
requirement → code → test mapping.

---

## 1. Live-execution boundary (LOCKED)

Live order placement is disabled by default (`HPM_LIVE_ENABLED=false`,
FR-LIVE-001) and **the flag alone is never sufficient**. The compliance gate
(`execution/live_adapter.py::ComplianceGate`) requires **all** of the following
to independently pass before anything could be submitted (FR-LIVE-002):

| Gate | Env / source | Default |
|---|---|---|
| Live enabled | `HPM_LIVE_ENABLED` | `false` |
| Operator age verified | `HPM_OPERATOR_AGE_VERIFIED` | `false` |
| Jurisdiction allowed | `HPM_OPERATOR_JURISDICTION_ALLOWED` | `false` |
| Risk acknowledged | `HPM_OPERATOR_ACKNOWLEDGED_RISK` | `false` |
| Red-team sign-off | `HPM_RED_TEAM_PASSED` (NFR-SEC-005) | `false` |
| Geoblock check | live geoblock endpoint, **fail-closed** | blocks on error |
| Signing key present | configured secret store | absent |

Additional guarantees:

- **Reference-only (FR-LIVE-004).** The live MCP tool accepts only a reference
  to a previously risk-approved intent — never raw price/size/market arguments.
  Until every gate passes it returns `blocked`.
- **Cancel-only always allowed (FR-LIVE-006).** Emergency cancellation is never
  gated behind the live switches.
- **Freeze on compliance change (FR-LIVE-008).** Any change to compliance state
  freezes the adapter.
- **Process isolation (NFR-SEC-007, optional).** `HPM_LIVE_PROCESS_ISOLATION=true`
  runs the locked adapter in its own OS process with a minimal line-delimited
  JSON IPC surface; secret material lives only in the child process.

The system does **not** bypass geoblocks, age, jurisdiction, platform terms, or
law. It ships with live execution disabled.

---

## 2. Secret handling

Secrets are **never** returned by any MCP tool, resource, dashboard endpoint,
log line, or audit export. Audit exports are redacted by key name
(`persistence/redact.py`, NFR-SEC-002 / NFR-PRIV-004).

Secrets are read as `HPM_SECRET_<NAME>` (uppercased), e.g.
`HPM_SECRET_LIVE_SIGNING_KEY`. Three interchangeable backends sit behind one
`SecretStore` protocol (`execution/secrets.py`, NFR-SEC-001):

| Backend (`HPM_SECRET_STORE`) | At rest | Notes |
|---|---|---|
| `env` (default) | process env only | paper trading needs no secrets |
| `encrypted_file` | **ciphertext** | Fernet; key derived from `HPM_SECRET_MASTER_PASSPHRASE` via PBKDF2-HMAC-SHA256 (390k iterations). Plaintext never touches disk. |
| `keyring` | OS keychain | Windows Credential Locker / macOS Keychain / Linux Secret Service; requires the optional `keyring` extra and degrades gracefully when unavailable. |

The `SigningVault` is the **only** component that reads key material. It signs
references via HMAC, is locked when no key is present, and **never returns the
key** to a caller (`status()["exposes_secrets"]` is always `False`).

> Rotate any leaked key immediately and re-provision it through the secret store.

---

## 3. Untrusted-input handling (prompt-injection defense)

All external text — market descriptions, X/social posts, news, weather/sports
feeds — is **sanitized and tagged untrusted** before it can reach the model
(`util/sanitize.py`, `signals/base.py`, NFR-SEC-004). Suspected prompt-injection
is flagged, and **tainted evidence is rejected by the risk engine** rather than
silently trusted. The sanitizer is hardened against tab/zero-width/homoglyph and
proximity-based bypass attempts (`tests/security/`).

Treat all market, social, and news content as adversarial data, never as
instructions.

---

## 4. MCP transport security

- **Strict schemas (MCP-SR-005 / NFR-SEC-003).** Every tool input is validated
  against a JSON Schema with `additionalProperties: false`; unknown or
  malformed fields are rejected (`mcp/tools.py`, `mcp/server.py::_validate`).
- **No shell from tool arguments (MCP-SR-004).** Tools dispatch to typed methods;
  there is no `subprocess`/`os.system` path from agent input.
- **stdio hygiene (MCP-SR-003).** On the default stdio transport, stdout is
  reserved for JSON-RPC and all logging goes to stderr.
- **HTTP transport (MCP-SR-002, opt-in).** When `HPM_MCP_HTTP_ENABLED=true`, the
  server binds localhost, enforces Origin/Host DNS-rebinding protection, and
  requires a bearer token (`HPM_MCP_HTTP_TOKEN`).

---

## 5. Dashboard security

The dashboard binds `127.0.0.1` by default (NFR-SEC-006). A token
(`HPM_DASHBOARD_TOKEN`) is **required** for all REST endpoints, `/metrics`, and
the `/ws` WebSocket whenever the host is not localhost
(`dashboard/server.py::_check_token`). Every money figure is labelled **PAPER**,
and stale/locked/emergency states are highlighted.

> Binding the dashboard or MCP HTTP server to a non-localhost address exposes it
> to your network. Always set a strong token and prefer a reverse proxy with TLS
> if remote access is genuinely required.

---

## 6. Audit integrity & privacy

- **Append-only, hash-chained audit log** (`audit/store.py`). Each event chains
  to the previous; `verify_chain` detects any tampering or reordering.
- **Audit per tool call** (NFR-OBS-001) with full intent traceability —
  snapshot id, policy version, and evidence references on every record.
- **Emergency stop** freezes new actions, cancels open paper orders, and records
  an audit event (AC-007).
- **Local by default** (NFR-PRIV-001): SQLite on disk, no external calls in
  synthetic mode. X/social data supports retention purge (NFR-PRIV-003), and
  audit exports are redacted (NFR-PRIV-004).

---

## 7. Supply chain

Dependencies are declared in `pyproject.toml` with lower-bound pins.
`cryptography` is a core dependency (the encrypted secret store needs it at
startup); `keyring` is an optional extra. Install only from trusted indexes and
review lockfile changes before deploying.

---

## Reporting a vulnerability

This is a local, paper-first research project. If you discover a security issue
(secret leakage, a live-gate bypass, audit-chain tampering, sanitizer bypass,
or transport auth weakness):

1. **Do not** open a public issue with exploit details.
2. Contact the maintainer privately with a minimal reproduction and the impacted
   component (file/function).
3. Allow reasonable time for a fix before any disclosure.

When in doubt, prefer the conservative action: keep live disabled, keep the
dashboard on localhost, and never commit secrets.
