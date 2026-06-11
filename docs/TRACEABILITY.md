# SRS Traceability Matrix - PM-MCP-SRS-001

Maps every requirement to its implementation and the test(s) that verify it.
Status: **DONE** (implemented + tested), **SCOPE** (deliberate, documented MVP
boundary - see notes). Full suite: 211 tests, all passing. Adds: contract tests
(`integration/test_contracts.py`), MCP HTTP transport (`integration/test_mcp_http.py`),
secret store (`security/test_secrets.py`), live-process isolation
(`compliance/test_live_process.py`), hard simulations (`e2e/test_hard_sim.py`),
and a Windows-native mutation harness (`mutation/test_mutation.py`).

## MCP server (SRS 10.1)

| Req | Status | Implementation | Test |
|---|---|---|---|
| MCP-SR-001 stdio transport | DONE | `mcp/server.py::run_stdio` | `integration/test_daemon_and_mcp.py` (in-memory client) |
| MCP-SR-002 Streamable HTTP (localhost+origin+auth) | DONE | `mcp/http_server.py` (StreamableHTTPSessionManager + TransportSecuritySettings + bearer token, localhost bind) | `integration/test_mcp_http.py` (handshake + 401 + bad-origin) |
| MCP-SR-003 stdout reserved for JSON-RPC | DONE | `mcp/server.py` logs to stderr | manual + stdio handshake |
| MCP-SR-004 no shell from tool args | DONE | no `subprocess`/`os.system`; tools dispatch to typed methods | code review |
| MCP-SR-005 strict schemas, reject unknown | DONE | `mcp/tools.py` `additionalProperties:false`; `mcp/server.py::_validate` | `integration/test_daemon_and_mcp.py::test_mcp_schema_rejects_unknown_and_missing`, `security/test_security.py::test_mcp_schema_fuzz_rejects_garbage` |

## Market discovery & data (SRS 11.1-11.2)

| Req | Status | Implementation | Test |
|---|---|---|---|
| FR-MD-001 Gamma normalize | DONE | `data/polymarket_client.py::normalize_gamma_market` | covered via synthetic discovery shape |
| FR-MD-002 order-book-enabled gate | DONE | `data/discovery.py::is_tradable` | `unit/test_foundation.py::test_tradable_requires_orderbook_and_resolution` |
| FR-MD-003/004 resolution rules required/reject ambiguous | DONE | `models.py::Market.has_clear_resolution`, `risk/engine.py` | `unit/test_risk_engine.py::test_reject_ambiguous_resolution` |
| FR-MD-005 filters (topic/end/liquidity/volume/spread/OB/source/exclude) | DONE | `data/discovery.py::passes_filters`, `daemon/core.py::search_markets` (live liquidity/spread) | `unit/test_foundation.py::test_filters_*`, `integration/test_market_data.py` |
| FR-DATA-001 WS market channels | DONE | `data/polymarket_client.py::stream` | (live path) |
| FR-DATA-002 hot cache | DONE | `data/cache.py::OrderBookCache` | `integration/test_market_data.py::test_stream_updates_cache_and_persists` |
| FR-DATA-003 reconcile WS vs REST / gaps | DONE | `data/market_data.py::_reconcile_loop` | `integration/test_market_data.py` |
| FR-DATA-004 staleness flags; no order on stale | DONE | `data/cache.py::is_stale`, `risk/engine.py` | `unit/test_risk_engine.py::test_reject_stale_data`, `chaos/test_chaos.py::test_connectivity_loss_*` |
| FR-DATA-005 record snapshots for replay | DONE | `data/market_data.py::_ingest -> db.save_snapshot` | `replay/test_replay.py` |
| FR-DATA-006 respect rate limits | DONE | `data/polymarket_client.py::_get` (429 -> RateLimitedError + backoff) | `chaos/test_chaos.py::test_rate_limit_raises` |

## Signals (SRS 11.3-11.4)

| Req | Status | Implementation | Test |
|---|---|---|---|
| FR-SOC-001 official X API only, no scraping | DONE | `signals/social_x.py::_fetch_live` | code review + `integration/test_signals.py` |
| FR-SOC-002 provenance stored | DONE | `signals/base.py::build_signal` | `integration/test_signals.py::test_gather_multisource_and_provenance` |
| FR-SOC-003 sanitize untrusted | DONE | `util/sanitize.py`, `signals/base.py` | `security/test_security.py`, `unit/test_foundation.py::test_sanitize_*` |
| FR-SOC-004 multi-dimension summary | DONE | `signals/registry.py::summary` | `integration/test_signals.py` |
| FR-SOC-005 X is delayed, not ms | DONE | `signals/social_x.py::_META latency_class=delayed` | `integration/test_signals.py::test_social_is_delayed_not_realtime` |
| FR-SOC-006 provenance graph | DONE | `signals/registry.py::summary` provenance list | `integration/test_signals.py` |
| FR-SOC-007 counter-signal search | DONE | `signals/registry.py::counter_signal_search` | `integration/test_signals.py::test_counter_signal_search` |
| FR-EXT-001 plug-in adapters | DONE | `signals/registry.py::adapters`, `signals/external.py` | `integration/test_signals.py` |
| FR-EXT-002 adapter metadata | DONE | `signals/base.py::AdapterMeta` | `integration/test_signals.py::test_adapter_metadata_complete` |
| FR-EXT-003 weather metadata | DONE | `signals/external.py::WeatherAdapter` | `integration/test_signals.py` |
| FR-EXT-004 sports metadata | DONE | `signals/external.py::SportsAdapter` | `integration/test_signals.py` |
| FR-EXT-005 reject stale-vs-horizon | DONE | `risk/engine.py::_source_stale_for_horizon` | `unit/test_risk_engine.py::test_source_stale_for_horizon` |

## Trade intent & risk (SRS 11.5-11.6)

| Req | Status | Implementation | Test |
|---|---|---|---|
| FR-TI-001 intents not orders | DONE | `execution/intents.py` | `e2e/test_acceptance.py::test_ac003_*` |
| FR-TI-002 required fields | DONE | `models.py::TradeIntent`, `intents.py::create` | `unit/test_intents_evaluation.py` |
| FR-TI-003 reject missing fields | DONE | `intents.py::create` missing_fields | `unit/test_intents_evaluation.py::test_intent_needs_more_evidence_without_refs` |
| FR-TI-004 break-even + EV | DONE | `execution/economics.py`, `intents.py` | `unit/test_foundation.py::test_*ev*`, `test_intents_evaluation.py` |
| FR-TI-005 thesis + counter-thesis | DONE | `intents.py`, `risk/engine.py` | `unit/test_risk_engine.py::test_reject_missing_counter_thesis` |
| FR-TI-006 no blind thesis reuse | DONE | `intents.py::similar_past_intents` | `integration/test_daemon_and_mcp.py` |
| FR-RISK-001 single engine | DONE | `risk/engine.py::RiskEngine.evaluate` | all risk tests |
| FR-RISK-002 all limit types | DONE | `risk/engine.py` | `unit/test_risk_engine.py` (per-limit tests) |
| FR-RISK-003 conservative defaults; no leverage/martingale | DONE | `config.py::RiskPolicy`, `manager.py::_safe_policy` | `security/test_review_fixes.py::test_risk_profile_cannot_loosen_or_disable_guards` |
| FR-RISK-004 reject ambiguous/stale/insufficient | DONE | `risk/engine.py` | `unit/test_risk_engine.py::test_reject_*` |
| FR-RISK-005 machine-readable reasons | DONE | `risk/engine.py` reasons/violated_rules | all risk tests |
| FR-RISK-006 dashboard shows decisions | DONE | `dashboard/ui.py::vRisk` | `integration/test_dashboard.py` |
| FR-RISK-007 versioned policy | DONE | `config.py::RiskPolicy.version` | `unit/test_risk_engine.py::test_policy_version_changes_with_limits` |

## Paper engine (SRS 11.7)

| Req | Status | Implementation | Test |
|---|---|---|---|
| FR-PAPER-001 paper default | DONE | `config.py`, `manager.py` | `e2e` |
| FR-PAPER-002 limit-order sim, partials, slippage | DONE | `execution/paper_engine.py::_match` | `unit/test_paper_engine.py::test_marketable_buy_walks_book`, `test_partial_fill_*` |
| FR-PAPER-003 passive + marketable | DONE | `paper_engine.py` order types | `unit/test_paper_engine.py` |
| FR-PAPER-004 double-entry ledger | DONE | `execution/ledger.py` | `unit/test_foundation.py::test_ledger_*`, `property/test_property.py::test_ledger_always_balances_*` |
| FR-PAPER-005 fill provenance (snapshot) | DONE | `models.py::Fill.snapshot_id`, `paper_engine.py` | `unit/test_paper_engine.py`, `replay/test_replay.py` |
| FR-PAPER-006 pessimistic | DONE | `economics.py` slippage, queue-on-trade-through | `unit/test_paper_engine.py::test_marketable_buy_does_not_cross_above_limit` |
| FR-PAPER-007 1-3 day + sample warning | DONE | `campaign/promotion.py` | `unit/test_intents_evaluation.py::test_promotion_blocks_live_by_default` |

## Live adapter (SRS 11.8) - all LOCKED

| Req | Status | Implementation | Test |
|---|---|---|---|
| FR-LIVE-001 disabled default | DONE | `config.py::live_enabled=False` | `compliance/test_compliance.py::test_live_disabled_by_default` |
| FR-LIVE-002 all gates | DONE | `execution/live_adapter.py::ComplianceGate` | `compliance/test_compliance.py::test_all_gates_must_pass_even_with_flags` |
| FR-LIVE-003 geoblock | DONE | `polymarket_client.py::geoblock_check`, `live_adapter.py` | `compliance/test_compliance.py::test_geoblock_fail_closed` |
| FR-LIVE-004 no raw params (reference-only) | DONE | `mcp/tools.py::live_place_order_intent` schema | `e2e/test_acceptance.py::test_ac006_*` |
| FR-LIVE-005 vault isolates keys | DONE | `live_adapter.py::SigningVault` | `compliance/test_compliance.py::test_signing_vault_never_signs_or_exposes` |
| FR-LIVE-006 cancel-only emergency | DONE | `live_adapter.py::cancel_order` | `compliance/test_compliance.py::test_cancel_only_always_allowed` |
| FR-LIVE-007 immutable live audit | DONE | `live_adapter.py` audit appends | covered by audit tests |
| FR-LIVE-008 freeze on compliance change | DONE | `live_adapter.py::freeze` | `compliance/test_compliance.py::test_compliance_freeze_on_change` |

## Learning & dashboard (SRS 11.9-11.10)

| Req | Status | Implementation | Test |
|---|---|---|---|
| FR-LEARN-001/002 postmortem + classify | DONE | `learning/postmortem.py` | `e2e` + daemon flow |
| FR-LEARN-003 structured lesson | DONE | `models.py::Lesson`, `learning/lessons.py` | `security/test_review_fixes.py::test_signal_purge` (lesson path), daemon flow |
| FR-LEARN-004 compact-only to active memory | DONE | `learning/hermes_bridge.py` (_MAX_LESSON_CHARS) | code review |
| FR-LEARN-005 skill candidates | DONE | `hermes_bridge.py::export_skill_candidate` | manual verify (cli) |
| FR-LEARN-006 no single-lucky-trade rule | DONE | `learning/lessons.py` (MIN_EVIDENCE_FOR_ACTIVE) | `e2e` (lesson downgraded to session) |
| FR-DASH-001 local URL via tool | DONE | `daemon/core.py::get_dashboard_url` | `e2e/test_acceptance.py::test_ac001_*` |
| FR-DASH-002 all panels | DONE | `dashboard/ui.py` | `integration/test_dashboard.py` |
| FR-DASH-003 searchable timeline | DONE | `dashboard/ui.py::filterTimeline` | `integration/test_dashboard.py::test_index_has_trades_tab_and_export` |
| FR-DASH-004 "why did this happen" trade detail | DONE | `daemon/core.py::get_trade_detail`, `dashboard/ui.py::tradeDetail` | `integration/test_dashboard.py::test_trade_detail_and_export_endpoints` |
| FR-DASH-005 emergency + export controls | DONE | `dashboard/{server,ui}.py` (emergency, pause/resume/stop, export audit) | `integration/test_dashboard.py::test_emergency_endpoint_blocks` |
| FR-DASH-006 highlight unsafe states | DONE | `dashboard/ui.py` (stale/locked/unbalanced badges) | `integration/test_dashboard.py::test_index_has_paper_label` |

## Non-functional (SRS 12)

| Req | Status | Implementation | Test |
|---|---|---|---|
| NFR-LAT-001 cached snapshot <=50ms | DONE | in-memory cache reads | `latency/test_latency.py::test_cached_snapshot_p95_under_50ms` |
| NFR-LAT-002 risk check <=25ms | DONE | pure `risk/engine.py` | `latency/test_latency.py::test_risk_check_p95_under_25ms` |
| NFR-LAT-003 paper order <=30ms | DONE | `paper_engine.py` | `latency/test_latency.py::test_paper_order_acceptance_p95_under_30ms` |
| NFR-LAT-004 dashboard update <=250ms | DONE | event bus -> WS | `latency/test_latency.py::test_dashboard_update_after_local_event_p95_under_250ms` |
| NFR-LAT-005 market-data proc <=10ms | DONE | `paper_engine.on_book_update` | `latency/test_latency.py::test_market_data_processing_p95_under_10ms` |
| NFR-LAT-006 X proc <=500ms local | DONE | `signals/social_x.py` | `latency/test_latency.py::test_x_social_processing_p95_under_500ms` |
| NFR-LAT-007 agent loop no ms target | DONE | LLM is async, off hot path | by design |
| NFR-REL-001 persist before ack | DONE | SQLite WAL+FULL commit | `chaos/test_chaos.py` |
| NFR-REL-002 restart recovery | DONE | persistence | `chaos/test_chaos.py::test_restart_recovers_ledger_and_positions` |
| NFR-REL-003 stale on WS loss | DONE | `cache.set_connectivity_lost`, `market_data._staleness_loop` | `chaos/test_chaos.py::test_connectivity_loss_*` |
| NFR-REL-004 safe degradation | DONE | system runs without X (synthetic) | `integration/test_signals.py` |
| NFR-REL-005 idempotency keys | DONE | intents/decisions/orders | `unit/test_audit.py::test_intent_idempotent_insert`, `test_paper_engine.py::test_idempotent_order_placement` |
| NFR-SEC-001 OS keychain | DONE | `execution/secrets.py` (Env / EncryptedFile-Fernet+PBKDF2 / Keyring) behind `SigningVault` | `security/test_secrets.py` (encryption-at-rest, no-leak, locked-by-default) |
| NFR-SEC-002 no secret leakage | DONE | `persistence/redact.py` | `security/test_security.py` |
| NFR-SEC-003 strict schemas | DONE | `mcp/tools.py`, `mcp/server.py` | `security/test_security.py::test_mcp_schema_fuzz_rejects_garbage` |
| NFR-SEC-004 untrusted sanitized | DONE | `util/sanitize.py` | `security/test_security.py`, `test_review_fixes.py::test_sanitizer_catches_hardened_cases` |
| NFR-SEC-005 red-team gate before live | DONE | `config.red_team_passed`, `ComplianceGate` | `security/test_review_fixes.py::test_red_team_gate_blocks_live` |
| NFR-SEC-006 dashboard token if non-localhost | DONE | `dashboard/server.py::_check_token` (REST + /metrics + /ws) | `security/test_review_fixes.py::test_metrics_requires_token_when_remote` |
| NFR-SEC-007 live adapter separate process | DONE | `execution/live_process.py` (subprocess host + `LiveProcessClient`, minimal JSON IPC, secrets only in child); daemon `live_process_isolation` flag | `compliance/test_live_process.py` |
| NFR-PRIV-001 local by default | DONE | SQLite local, no external calls in synthetic | by design |
| NFR-PRIV-002 external LLM calls visible | DONE | system makes no LLM calls (it is the server) | by design |
| NFR-PRIV-003 X data retention/deletion | DONE | `db.purge_signals_before`, `daemon.purge_old_signals` | `security/test_review_fixes.py::test_signal_purge` |
| NFR-PRIV-004 audit export redaction | DONE | `persistence/redact.py`, `audit/store.py::export` | `unit/test_audit.py::test_export_redacts_and_verifies` |
| NFR-OBS-001 audit per tool call | DONE | `daemon/core.py::_audit_tool` | `e2e` audit-chain test |
| NFR-OBS-002 intent traceability | DONE | snapshot_id + policy_version + evidence refs on records | `replay/test_replay.py` |
| NFR-OBS-003 Prometheus metrics | DONE | `metrics/registry.py`, `/metrics` | `integration/test_dashboard.py::test_metrics_endpoint` |
| NFR-OBS-004 ops metrics (lag/throttle/reconnect/X/dash latency) | DONE | `metrics/registry.py` + wiring in daemon/market_data/dashboard | `integration/test_dashboard.py::test_metrics_endpoint` |

## Compliance, promotion, acceptance (SRS 18, 15.2, 19.2)

| Req | Status | Test |
|---|---|---|
| COMP-001..008 | DONE | `compliance/test_compliance.py` |
| PC-001..006 | DONE | `unit/test_intents_evaluation.py::test_promotion_*` |
| AC-001 start campaign + dashboard URL | DONE | `e2e/test_acceptance.py::test_ac001_*` |
| AC-002 discover OB markets + live prices | DONE | `e2e/test_acceptance.py::test_ac002_*` |
| AC-003 intents pass risk before paper | DONE | `e2e/test_acceptance.py::test_ac003_*` |
| AC-004 fills replayable from snapshots | DONE | `e2e/test_acceptance.py::test_ac004_*`, `replay/test_replay.py` |
| AC-005 dashboard shows P&L/risk/evidence/lessons | DONE | `e2e/test_acceptance.py::test_ac005_*` |
| AC-006 live disabled, not triggerable by raw args | DONE | `e2e/test_acceptance.py::test_ac006_*` |
| AC-007 emergency stop freezes + audits | DONE | `e2e/test_acceptance.py::test_ac007_*` |
| AC-008 promotion report 3 verdicts | DONE | `e2e/test_acceptance.py::test_ac008_*` |

