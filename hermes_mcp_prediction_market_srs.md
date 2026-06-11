# Software Requirements Specification: Hermes-Integrated MCP Prediction Market Agent
**Local, low-latency paper-trading-first system for Polymarket-style prediction markets**

**Document ID:** PM-MCP-SRS-001  
**Version:** 0.1 Draft  
**Date:** 2026-05-30

# 1. Executive Summary

This Software Requirements Specification defines a local MCP-based system that lets an LLM agent, such as Hermes Agent, Codex, Kira, or an IDE agent, research and operate a prediction-market workflow through safe, structured tools. The product is not a simple exchange wrapper. It is an Agent Trading Operating Layer with four strict properties: paper-first validation, deterministic risk gates, auditable learning, and a live execution boundary that cannot be crossed without legal eligibility, jurisdiction checks, and explicit operator authorization.

The system is designed around a key architectural rule: the LLM agent must not sit on the millisecond-critical path. Market-data ingestion, order-book state, paper fills, risk checks, and execution adapters run inside a local event-driven daemon. The MCP server exposes fast cached state and controlled commands to the agent. This allows an agent to reason, explain, supervise, and improve, while the core engine preserves low latency, deterministic validation, and complete auditability.

The MVP focuses on paper trading. Live trading is specified only as a compliance-gated adapter for legally eligible operators in permitted jurisdictions. The system must not bypass geoblocks, age restrictions, platform rules, API rate limits, X policies, or local law. The real-money adapter remains disabled until eligibility and jurisdiction gates pass. This is especially important because prediction markets are treated as gambling or regulated financial activity in many jurisdictions, and Spain announced a domestic block and licence investigation against Polymarket and Kalshi on 2026-05-26 [S18].

# 2. Document Control

|Field|Value|
|---|---|
|Document ID|PM-MCP-SRS-001|
|Version|0.1 Draft|
|Date|2026-05-30|
|Status|Concept SRS / architecture draft|
|Primary language|English|
|Primary runtime target|Local machine or local network host|
|Primary client|MCP-capable LLM agent running in an IDE, terminal, or Hermes Agent environment|
|Primary trading mode|Paper trading by default|
|Live trading mode|Disabled until compliance, age, jurisdiction, and explicit authorization gates pass|

# 3. Source Notes and Current Platform Facts

This SRS relies on publicly available documentation and news available on 2026-05-30. Source IDs are used inside the requirements text for traceability.

|ID|Source|URL|
|---|---|---|
|S1|Model Context Protocol Specification, version 2025-11-25|https://modelcontextprotocol.io/specification/2025-11-25|
|S2|Model Context Protocol Transports, stdio and Streamable HTTP|https://modelcontextprotocol.io/specification/2025-03-26/basic/transports|
|S3|Model Context Protocol Server Tools Specification|https://modelcontextprotocol.io/specification/2025-11-25/server/tools|
|S4|Model Context Protocol Server Resources Specification|https://modelcontextprotocol.io/specification/2025-11-25/server/resources|
|S5|Polymarket API Introduction: Gamma, Data, CLOB, Bridge APIs|https://docs.polymarket.com/api-reference/introduction|
|S6|Polymarket Market Data Overview: events, markets, outcomes, prices, public endpoints|https://docs.polymarket.com/market-data/overview|
|S7|Polymarket Trading Overview: CLOB, signatures, authentication|https://docs.polymarket.com/trading/overview|
|S8|Polymarket WebSocket Overview and Market Channel|https://docs.polymarket.com/market-data/websocket/overview|
|S9|Polymarket Create Order documentation|https://docs.polymarket.com/trading/orders/create|
|S10|Polymarket API Rate Limits|https://docs.polymarket.com/api-reference/rate-limits|
|S11|Polymarket Geographic Restrictions Help Center and API geoblock guidance|https://help.polymarket.com/en/articles/13364163-geographic-restrictions|
|S12|X API Introduction and public conversation access|https://docs.x.com/x-api/introduction|
|S13|X API Filtered Stream documentation|https://docs.x.com/x-api/posts/filtered-stream/introduction|
|S14|X API Rate Limits|https://docs.x.com/x-api/fundamentals/rate-limits|
|S15|X Developer Policy and Automation Rules|https://docs.x.com/developer-terms/policy|
|S16|Hermes Agent Documentation|https://hermes-agent.nousresearch.com/docs/|
|S17|Hermes Agent Persistent Memory documentation|https://github.com/NousResearch/hermes-agent/blob/main/website/docs/user-guide/features/memory.md|
|S18|The Guardian: Spain blocks access to Polymarket and Kalshi as it launches gambling licence investigation, 2026-05-26|https://www.theguardian.com/world/2026/may/26/spain-blocks-access-polymarket-kalshi-gambling-licence-investigation|

# 4. Product Vision

The product is a local, low-latency MCP server plus supporting services that turn an LLM agent into a supervised prediction-market research and paper-trading operator. The agent can discover markets, analyze live prices, inspect social and external evidence, propose trade intents, execute simulated orders, learn from outcomes, and produce a promotion report after a controlled paper campaign.

The differentiator is not that the agent can call Polymarket APIs. A normal MCP wrapper can already expose market search, prices, and order endpoints. The differentiator is that this system creates a complete local control plane around the agent: paper/live parity, dashboard telemetry, deterministic risk gates, source provenance, lesson memory, replayable audit logs, and a promotion process that prevents moving from paper to live based on hype or a short lucky streak.

## 4.1 Core Product Promise
- The operator gives a high-level mission to the agent, such as “run a 48-hour paper campaign on liquid weather and sports markets with a paper bankroll of 1,000 USD and maximum 1% risk per decision.”
- The agent uses MCP tools to research, propose, simulate, and paper-trade while a local daemon handles market data, paper fills, order-book state, and risk enforcement.
- The operator receives a local dashboard URL showing live paper portfolio, market watchlists, evidence, agent rationales, risk rejections, fills, P&L, drawdown, and learning notes.
- After the campaign, the system creates a promotion report. The report may recommend continuing paper mode, changing constraints, or enabling live mode only if legal, compliant, and explicitly approved.

## 4.2 Non-Negotiable Product Constraints
- No guaranteed profit claims. The product evaluates decisions; it does not promise money.
- No geoblock bypass, VPN bypass, sanction bypass, age bypass, or hidden real-money execution.
- No scraping or browser scripting against X. Public posts must be obtained through permitted X API access and stored/displayed according to X policy [S12][S15].
- No direct private-key exposure to the LLM or to MCP tool arguments.
- No unrestricted live order tool. Live execution is a separate adapter behind risk, compliance, and confirmation gates.
- No LLM-only trading. Every trade intent must pass deterministic validation before paper or live execution.

# 5. Glossary

|Term|Definition|
|---|---|
|MCP|Model Context Protocol, a JSON-RPC based protocol for exposing tools, resources, and prompts to LLM applications [S1].|
|Host|The user-facing LLM application or IDE that initiates MCP connections.|
|Client|The connector inside the host that communicates with one MCP server.|
|MCP Server|The local program exposing prediction-market tools, resources, and prompts to the agent.|
|Hermes Agent|A self-improving, self-hosted agent framework with memory, skills, subagents, and MCP support [S16].|
|Campaign|A bounded paper-trading or live-trading run with explicit time, bankroll, market universe, risk limits, and evaluation criteria.|
|Trade Intent|A structured proposal produced by the agent, not an order. It includes market, side, size, price, evidence, confidence, risk rationale, and expiration.|
|Risk Decision|A deterministic approve/reject/modify decision produced by the risk engine after validating a trade intent.|
|Paper Fill|A simulated fill generated from order-book replay and configured fill rules.|
|Live Adapter|The compliance-gated module that may sign and submit real orders only after all policy checks pass.|
|Fast Lane|Non-LLM event-driven path for cached market data, risk checks, paper fills, and execution-state updates.|
|Agent Lane|LLM reasoning path for research, explanations, strategy hypotheses, and postmortems.|

# 6. System Context

## 6.1 MCP Context

MCP servers can expose resources, prompts, and tools to LLM hosts [S1]. Tools are model-controlled and can interact with external systems, but the MCP specification recommends human-in-the-loop control and clear tool invocation visibility for trust and safety [S3]. Resources are application-driven context identified by URIs and can represent files, database records, or application-specific state [S4]. For local integrations, stdio is the preferred transport; Streamable HTTP is also supported, but local HTTP servers must bind to localhost, validate origins, and use authentication to reduce DNS-rebinding and local network exposure [S2].

## 6.2 Polymarket Context

Polymarket exposes separate API domains: Gamma API for market discovery, Data API for positions/trades/analytics, and CLOB API for order books, prices, price history, and trading operations; public market data does not require authentication, while order management requires authentication [S5][S6]. Its CLOB is described as hybrid-decentralized: offchain matching with onchain settlement, non-custodial trading, EIP-712 signed orders, and Polygon settlement [S7]. WebSocket channels provide near-real-time order-book data, trades, personal order activity, sports events, and other live data streams [S8].

## 6.3 X / Twitter Context

The X API provides programmatic access to public conversation, search, public posts, users, trends, and near-real-time streaming [S12]. The Filtered Stream endpoint receives posts matching rules through a persistent stream and documents approximately 6-7 seconds of P99 latency, meaning X data is useful for social intelligence but should not be treated as a sub-second signal unless a lower-latency permitted product is available [S13]. X rate limits and automation policies must be respected [S14][S15].

## 6.4 Hermes Agent Context

Hermes Agent is relevant because it provides persistent memory, skills, subagents, scheduled automations, and MCP support [S16]. Its built-in memory is intentionally small and curated; MEMORY.md and USER.md are injected at session start, while session search stores past conversations in SQLite FTS5 and can retrieve previous discussions [S17]. This SRS treats Hermes as the learning/orchestration layer, not the execution authority.

# 7. Scope

## 7.1 In Scope
- Local MCP server exposing prediction-market tools, resources, and prompts.
- Local event-driven core daemon for market data, state caching, risk checks, paper execution, and audit logs.
- Paper trading engine with order-book based fill simulation, slippage model, and portfolio ledger.
- Dashboard served locally, showing campaign progress, evidence, decisions, orders, fills, risk, P&L, and lessons.
- Social intelligence ingestion through official X API access, with source provenance and policy-respecting storage.
- External data adapters for weather, sports, news, oracle/resolution data, and market-specific official sources.
- Hermes Agent integration through MCP tools/resources and a lesson-writing workflow compatible with memory and skills.
- Live execution adapter design, but locked behind eligibility, jurisdiction, compliance, and confirmation gates.
- Replay/testing environment for historical market data, paper/live parity, and postmortem analysis.

## 7.2 Out of Scope
- Building a system to bypass Polymarket geoblocks or local law.
- Providing a guaranteed profitable trading strategy.
- Creating bots that manipulate markets, spam X, or coordinate false information.
- Training or fine-tuning foundation/frontier models on X posts unless explicitly licensed.
- High-frequency co-location trading. The system is latency-aware, but it is not a true HFT platform.
- Unrestricted autonomous real-money trading for minors, restricted jurisdictions, or users without legal eligibility.

# 8. Operating Modes

|Mode|Purpose|Allowed Actions|Blocked Actions|
|---|---|---|---|
|Research Mode|Market discovery and evidence review.|Fetch markets, prices, order books, external evidence, X summaries, resolution rules.|Paper fills, live orders, wallet actions.|
|Paper Mode|Simulated trading with realistic fills and risk controls.|Create campaign, paper orders, cancellations, simulated fills, dashboards, postmortems.|Live order signing/submission.|
|Review Mode|Evaluate paper campaign and decide whether constraints should change.|Generate reports, analyze failures, update lessons, propose configuration changes.|Live execution without explicit promotion and compliance gates.|
|Live-Eligible Mode|Compliance-gated real execution for legally eligible operators only.|Submit risk-approved order intents to live adapter after confirmation policy is satisfied.|Any trading if age, jurisdiction, platform, KYC/AML, or user consent checks fail.|
|Emergency Mode|Stop unsafe activity immediately.|Cancel open paper/live orders where permitted, freeze campaigns, export audit logs.|New orders, risk-limit relaxation, automatic restart.|

# 9. High-Level Architecture

The system is divided into five lanes. This separation is the main design decision that makes the product fast, safe, and agent-friendly.

|Lane|Components|Responsibility|
|---|---|---|
|Data Plane|Market Data Engine, X/Social Engine, External Data Adapters, Event Bus|Ingest raw and live data, normalize events, maintain hot caches.|
|Execution Plane|Paper Engine, Risk Engine, Live Adapter, Signing Vault|Turn approved trade intents into paper fills or, if enabled, live execution.|
|Control Plane|MCP Server, Prompts, Resources, Tool Registry|Expose safe operations and state to the LLM agent.|
|Intelligence Plane|LLM Agent, Hermes Memory, Subagents, Strategy Evaluators|Research markets, propose intents, critique decisions, write lessons.|
|Observation Plane|Dashboard, Metrics, Logs, Audit Store, Replay Tools|Show every decision and make every action reproducible.|

```text
+----------------------+       MCP stdio/localhost HTTP       +-----------------------+
| IDE / Hermes / Agent | <----------------------------------> | Prediction MCP Server |
+----------+-----------+                                      +-----------+-----------+
           |                                                          |
           | tools/resources/prompts                                  | local RPC
           v                                                          v
+----------------------+       event bus / shared cache        +-----------------------+
| Agent Intelligence   | <----------------------------------> | Core Trading Daemon   |
| - research           |                                      | - order books cache   |
| - trade intents      |                                      | - risk engine         |
| - postmortems        |                                      | - paper fills         |
| - lesson writing     |                                      | - live adapter lock   |
+----------+-----------+                                      +-----------+-----------+
           |                                                          |
           | lessons/resources                                        | upstream APIs / WSS
           v                                                          v
+----------------------+                                      +-----------------------+
| Hermes Memory/Skills |                                      | Polymarket / X / Data |
+----------------------+                                      +-----------------------+
           |
           v
+----------------------+       local websocket/SSE             +-----------------------+
| Operator Dashboard   | <----------------------------------- | Metrics + Audit Store |
+----------------------+                                      +-----------------------+
```

## 9.1 Component List

|Component|Description|MVP Priority|
|---|---|---|
|MCP Server|Exposes tools, resources, and prompts. It should be thin, deterministic, and not own private keys.|P0|
|Core Trading Daemon|Owns market cache, risk engine, paper/live state, and local RPC. Written in Rust or Go for predictable latency.|P0|
|Market Data Engine|Maintains subscriptions to CLOB WebSocket market channel and REST fallback snapshots.|P0|
|Market Discovery Engine|Uses Gamma API and CLOB metadata to build eligible market universe.|P0|
|Paper Trading Engine|Simulates order placement, partial fills, queue position, slippage, cancellations, and portfolio accounting.|P0|
|Risk Engine|Validates every trade intent before paper or live execution.|P0|
|Dashboard Server|Serves local web UI and streams state updates to browser.|P0|
|Audit Store|Append-only log for data snapshots, decisions, risk checks, orders, fills, lessons, and config changes.|P0|
|Social Intelligence Engine|Uses official X API and optional approved providers to summarize public conversation and extract signals.|P1|
|External Signal Adapters|Weather, sports, news, official resolution sources, price oracles, and market-specific data.|P1|
|Hermes Learning Bridge|Writes compact lessons, skill candidates, and postmortem summaries for Hermes memory/search.|P1|
|Replay Engine|Replays historical data and paper campaigns to test strategies and risk rules.|P1|
|Live Adapter|Signs and submits orders only after eligibility/compliance/user confirmation gates pass.|P2 / locked|

## 9.2 Fast Lane vs Agent Lane

The Fast Lane must process high-frequency state without LLM calls. It uses hot caches and deterministic rules. The Agent Lane can be slower because it handles research, reasoning, and learning. The MCP server bridges the two lanes but does not replace the Fast Lane.

|Operation|Lane|Target Behavior|
|---|---|---|
|Market data message normalization|Fast Lane|Process immediately after upstream receipt; no LLM call.|
|Best bid/ask cache read|Fast Lane through MCP resource/tool|Return from memory cache.|
|Risk check|Fast Lane|Deterministic approve/reject/modify.|
|Trade intent creation|Agent Lane|LLM creates structured proposal using evidence.|
|Paper fill simulation|Fast Lane|Uses order-book state and configured fill policy.|
|Postmortem and lesson writing|Agent Lane|LLM critiques closed or rejected decisions.|

# 10. MCP Server Requirements

## 10.1 MCP Transport
- MCP-SR-001: The MCP server SHALL support stdio transport for local IDE/agent use because stdio is simple and preferred for local integrations [S2].
- MCP-SR-002: If Streamable HTTP is enabled, the server SHALL bind only to 127.0.0.1 by default, validate Origin headers, require authentication, and reject non-local access unless explicitly configured [S2].
- MCP-SR-003: The MCP server SHALL keep stdout reserved for valid MCP JSON-RPC messages only when using stdio.
- MCP-SR-004: The MCP server SHALL never execute arbitrary shell commands from tool arguments.
- MCP-SR-005: The MCP server SHALL expose tool schemas with strict input validation and structured outputs.

## 10.2 Tool Categories

|Category|Tools|
|---|---|
|System|get_system_status, get_config, update_config, get_dashboard_url, emergency_stop|
|Market Discovery|search_markets, get_market_details, get_resolution_rules, build_watchlist|
|Market Data|get_market_snapshot, get_order_book, subscribe_markets, get_price_history, get_liquidity_summary|
|Social / External Signals|get_social_signal_summary, get_source_evidence, get_weather_signal_summary, get_sports_signal_summary|
|Campaign Control|start_paper_campaign, pause_campaign, resume_campaign, stop_campaign, get_campaign_report|
|Trading Intent|propose_trade_intent, simulate_trade_intent, risk_check_trade_intent, explain_risk_rejection|
|Paper Execution|paper_place_order, paper_cancel_order, paper_get_orders, paper_get_portfolio, paper_mark_to_market|
|Live Execution|live_place_order_intent, live_cancel_order, live_get_open_orders; all locked by compliance and confirmation gates|
|Learning|write_lesson, list_lessons, search_past_decisions, generate_postmortem, create_skill_candidate|
|Audit|get_audit_events, export_campaign_audit, replay_decision|

## 10.3 Required MCP Resources

|URI Pattern|Description|Update Mode|
|---|---|---|
|system://status|System health, upstream connectivity, active campaigns, mode, locks.|Dynamic|
|campaign://{campaign_id}/summary|Campaign state, constraints, P&L, drawdown, violations, promotion readiness.|Dynamic / subscribable|
|market://{market_id}|Normalized market metadata, outcomes, rules, liquidity, source status.|Dynamic|
|orderbook://{token_id}|Best bid/ask, depth, spread, last update time, staleness flags.|Dynamic / hot cache|
|portfolio://paper/{campaign_id}|Paper positions, orders, fills, realized/unrealized P&L.|Dynamic|
|risk://limits/{campaign_id}|Active risk policy, remaining capacity, exposure clusters.|Dynamic|
|signals://{market_id}/social|Sanitized social signal summary with source IDs and confidence.|Dynamic|
|lessons://campaign/{campaign_id}|Lessons, failure modes, skill candidates, memory suggestions.|Append-only|
|audit://event/{event_id}|Immutable audit event including inputs, outputs, timestamps, and hashes.|Static|

## 10.4 Required MCP Prompts

|Prompt|Purpose|Inputs|
|---|---|---|
|research_market|Guide the agent through market rules, liquidity, resolution source, price history, social evidence, and counterarguments.|market_id, horizon, allowed_sources|
|paper_campaign_manager|Run a paper campaign under constraints, propose intents, call risk check, and update dashboard.|campaign_id, bankroll, duration, market_universe, limits|
|trade_intent_reviewer|Critique one trade intent before execution and search for missing evidence or correlated exposure.|trade_intent_id|
|postmortem_closed_trade|Analyze a closed trade, classify failure/success, and propose one compact lesson.|trade_id, campaign_id|
|promotion_report|Decide whether paper results justify more paper testing or live eligibility review.|campaign_id, evaluation_window|
|live_supervisor|Operate in live-eligible mode with maximum conservatism, explicit consent, and full audit.|campaign_id, confirmation_policy|

## 10.5 Representative Tool Schemas

The exact JSON Schema will be implementation-specific, but the following shapes are required. The live tool intentionally accepts only a reference to a previously risk-approved trade intent; it does not accept arbitrary market, side, size, and price from the agent.

```text
Tool: start_paper_campaign
Input:
  campaign_name: string
  duration_hours: number
  paper_bankroll_usd: number
  market_filters: object
  risk_profile: object
  allowed_signal_sources: string[]
Output:
  campaign_id: string
  dashboard_url: string
  active_limits: object
  status: "running" | "rejected"

Tool: propose_trade_intent
Input:
  campaign_id: string
  market_id: string
  outcome: "YES" | "NO" | string
  side: "BUY" | "SELL"
  limit_price: number
  max_size_usd: number
  thesis: string
  evidence_refs: string[]
  confidence: number
  expires_at: string
Output:
  trade_intent_id: string
  normalized_ev: number | null
  missing_fields: string[]
  status: "created" | "needs_more_evidence" | "rejected_schema"

Tool: risk_check_trade_intent
Input:
  trade_intent_id: string
Output:
  decision: "approve" | "modify" | "reject"
  reasons: string[]
  approved_price: number | null
  approved_max_size_usd: number | null
  required_confirmations: string[]

Tool: paper_place_order
Input:
  trade_intent_id: string
  risk_decision_id: string
Output:
  paper_order_id: string
  status: "accepted" | "rejected" | "partially_filled" | "filled"
  simulated_fills: object[]
  portfolio_delta: object

Tool: live_place_order_intent
Input:
  trade_intent_id: string
  risk_decision_id: string
  user_confirmation_token: string
Output:
  status: "blocked" | "submitted" | "rejected" | "pending_manual_review"
  compliance_state: object
  order_ref: string | null
```

# 11. Functional Requirements

## 11.1 Market Discovery and Metadata
- FR-MD-001: The system SHALL discover events and markets through the Gamma API and normalize events, markets, outcomes, outcome prices, token IDs, condition IDs, question IDs, tags, and resolution rules [S5][S6].
- FR-MD-002: The system SHALL mark whether each market is order-book enabled before including it in a tradable universe [S6].
- FR-MD-003: The system SHALL store the exact resolution criteria and source links for every market before allowing trade intents.
- FR-MD-004: The system SHALL reject markets with missing, ambiguous, or unparseable resolution rules unless the operator manually allows research-only tracking.
- FR-MD-005: The system SHALL support market filters by topic, end time, liquidity, volume, spread, order-book availability, resolution source type, and excluded categories.

## 11.2 Market Data Ingestion
- FR-DATA-001: The system SHALL use Polymarket WebSocket market channels for live order-book snapshots, price changes, last trade prices, and lifecycle events where available [S8].
- FR-DATA-002: The system SHALL keep a hot in-memory order-book cache for subscribed tokens.
- FR-DATA-003: The system SHALL periodically reconcile WebSocket state with REST snapshots to detect missed events, stale books, and sequence gaps.
- FR-DATA-004: The system SHALL expose staleness flags to the agent and risk engine. No order may be approved against stale data.
- FR-DATA-005: The system SHALL record enough market-data snapshots to replay every paper fill and every live decision.
- FR-DATA-006: The system SHALL respect upstream rate limits and treat delayed/queued API responses as a risk condition, because Polymarket documents Cloudflare throttling under rate limits [S10].

## 11.3 Social Intelligence and X Analysis
- FR-SOC-001: The system SHALL ingest X data only through permitted X API access, such as search or Filtered Stream, and SHALL NOT script the X website or attempt to circumvent rate limits [S12][S14][S15].
- FR-SOC-002: The system SHALL store source identifiers, timestamps, author metadata permitted by policy, query rules, and retrieval method for every social evidence item.
- FR-SOC-003: The system SHALL sanitize all social text as untrusted input before exposing it to the LLM agent to reduce prompt-injection risk.
- FR-SOC-004: The system SHALL summarize social signals by novelty, source class, source credibility, stance, disagreement, velocity, and relevance to a specific market rule.
- FR-SOC-005: The system SHALL not treat X as a millisecond-level signal by default. X Filtered Stream has documented P99 latency in seconds, so social intelligence should be used for context, event awareness, and thesis review unless a permitted lower-latency product is configured [S13].
- FR-SOC-006: The system SHALL maintain a source provenance graph so the dashboard can show which posts, accounts, or linked sources influenced a decision.
- FR-SOC-007: The system SHALL support “counter-signal search”: for every bullish or bearish thesis, the agent must request contradictory evidence before placing a trade intent above configurable size thresholds.

## 11.4 External Signal Adapters
- FR-EXT-001: The system SHALL support plug-in adapters for weather, sports, crypto, macro, news, official government data, and other market-specific resolution sources.
- FR-EXT-002: Each adapter SHALL declare latency, update frequency, source authority, reliability, licensing, and whether it is suitable for real-time or delayed analysis.
- FR-EXT-003: Weather adapters SHALL preserve station/location metadata, forecast issue time, model run time, units, and confidence intervals.
- FR-EXT-004: Sports adapters SHALL preserve game status, official source, clock/period state, and update time.
- FR-EXT-005: The risk engine SHALL reject trade intents if a thesis relies on an external source that is stale relative to the market horizon.

## 11.5 Trade Intent Creation
- FR-TI-001: The agent SHALL create trade intents, not direct orders.
- FR-TI-002: A trade intent SHALL include market ID, token/outcome, side, limit price, maximum size, expiration, thesis, confidence, evidence references, expected edge, and invalidation criteria.
- FR-TI-003: The MCP server SHALL reject trade intents that omit evidence references, market rules, size, price, or expiration.
- FR-TI-004: The system SHALL compute break-even probability and expected value using the current execution price, slippage assumptions, and configured cost model.
- FR-TI-005: The agent SHALL provide at least one explicit reason why the market price may be wrong and one explicit reason why the agent may be wrong.
- FR-TI-006: The agent SHALL not reuse a previous thesis blindly. If similar past decisions exist, it must retrieve them and state whether conditions are comparable.

## 11.6 Risk Engine
- FR-RISK-001: Every paper and live trade intent SHALL pass through the same deterministic risk engine.
- FR-RISK-002: The risk engine SHALL enforce maximum order size, maximum market exposure, maximum category exposure, maximum correlated exposure, daily loss stop, campaign loss stop, liquidity threshold, spread threshold, staleness threshold, and source quality threshold.
- FR-RISK-003: The default configuration SHALL use conservative limits: small maximum order size, tight daily loss stop, no leverage/margin assumptions, no martingale, and no automatic size increases after losses.
- FR-RISK-004: The risk engine SHALL reject trades when market rules are ambiguous, market data is stale, order book depth is insufficient, social evidence is unsourced, or the external resolution source is unavailable.
- FR-RISK-005: The risk engine SHALL output machine-readable reasons for every rejection or modification.
- FR-RISK-006: The dashboard SHALL display risk decisions, rejected intents, and modified sizes/prices as first-class events.
- FR-RISK-007: Risk limits SHALL be versioned. Every trade decision must record the exact risk policy version used.

## 11.7 Paper Trading Engine
- FR-PAPER-001: Paper mode SHALL be the default operating mode.
- FR-PAPER-002: Paper fills SHALL simulate limit-order behavior against the local order-book state, including partial fills, queue assumptions, slippage, spread crossing, and cancellation timing.
- FR-PAPER-003: The paper engine SHALL support both passive limit orders and marketable limit orders, because Polymarket order documentation describes market orders as limit orders with marketable prices [S9].
- FR-PAPER-004: The paper engine SHALL maintain a double-entry ledger of paper cash, positions, orders, fills, fees/cost assumptions, realized P&L, unrealized P&L, and mark-to-market value.
- FR-PAPER-005: The paper engine SHALL record why each fill happened and which order-book snapshot was used.
- FR-PAPER-006: The paper engine SHALL be intentionally pessimistic by default: it should prefer conservative fills over optimistic fills to reduce false confidence.
- FR-PAPER-007: Paper mode SHALL support 1-day, 2-day, and 3-day campaigns, but the promotion report must warn when sample size is too small for statistical confidence.

## 11.8 Live Adapter
- FR-LIVE-001: The live adapter SHALL be disabled by default.
- FR-LIVE-002: The live adapter SHALL require legal eligibility, age eligibility, jurisdiction eligibility, platform eligibility, geoblock check, operator confirmation, and risk approval before any order submission.
- FR-LIVE-003: The system SHALL call the platform geoblock endpoint or equivalent availability check before order placement and SHALL reject orders from blocked regions [S11].
- FR-LIVE-004: The live adapter SHALL never accept raw arbitrary order parameters from the LLM. It may only receive a reference to a risk-approved trade intent.
- FR-LIVE-005: The signing vault SHALL isolate private keys from the MCP server and LLM. The agent must never see private keys, API secrets, seed phrases, or wallet recovery material.
- FR-LIVE-006: The live adapter SHALL support cancel-only mode for emergency shutdown and close-only regulatory states where supported.
- FR-LIVE-007: The live adapter SHALL record immutable audit events for confirmations, signatures, submissions, exchange responses, partial fills, cancellations, and errors.
- FR-LIVE-008: If any compliance state changes during a campaign, live mode SHALL freeze and require manual review before resuming.

## 11.9 Hermes Learning and Self-Correction
- FR-LEARN-001: The system SHALL create a postmortem for every closed position, major missed opportunity, large rejection, and rule violation.
- FR-LEARN-002: The postmortem SHALL classify outcome drivers: thesis correct/incorrect, timing error, liquidity error, source error, resolution-rule error, social hype, stale data, risk limit, or random variance.
- FR-LEARN-003: The system SHALL generate compact lessons in a structured format: trigger, observation, mistake or success pattern, new rule, valid-until condition, and source references.
- FR-LEARN-004: The Hermes bridge SHALL write only compact, generalizable lessons into active memory. Raw logs and large transcripts belong in the audit store or session search, not MEMORY.md.
- FR-LEARN-005: The system SHALL support skill candidates: reusable procedures that Hermes may convert into skills after repeated successful use.
- FR-LEARN-006: The system SHALL prevent “learning” from a single lucky trade by requiring repeated evidence or human confirmation before turning a one-off result into a durable rule.

## 11.10 Dashboard
- FR-DASH-001: The dashboard SHALL be served locally and exposed through an MCP tool returning a localhost URL.
- FR-DASH-002: The dashboard SHALL show active campaign, paper bankroll, P&L, drawdown, exposures, open orders, fills, watchlist, agent intents, risk decisions, source evidence, and lessons.
- FR-DASH-003: The dashboard SHALL show a timeline where every agent action, tool call, market data event, risk decision, order, fill, and postmortem is searchable.
- FR-DASH-004: The dashboard SHALL provide a “why did this happen?” panel for each trade, including price snapshot, thesis, evidence, counterarguments, risk policy, and outcome.
- FR-DASH-005: The dashboard SHALL provide emergency controls: pause agent, stop campaign, freeze live adapter, cancel open orders, export audit.
- FR-DASH-006: The dashboard SHALL highlight unsafe states: stale data, high drawdown, repeated rejections, high correlation, insufficient sample size, or compliance lock.

# 12. Non-Functional Requirements

## 12.1 Latency Requirements

Latency requirements are split between local processing targets and upstream-dependent targets. The system can control local overhead, but it cannot guarantee upstream network latency, API throttling, WebSocket delivery, X delivery, or blockchain settlement latency.

|Requirement|Target|Notes|
|---|---|---|
|NFR-LAT-001: Cached market snapshot via MCP|p95 <= 50 ms|No external network call allowed on hot path.|
|NFR-LAT-002: Risk check for one trade intent|p95 <= 25 ms|Pure deterministic local computation.|
|NFR-LAT-003: Paper order acceptance|p95 <= 30 ms|After risk approval; excludes dashboard rendering.|
|NFR-LAT-004: Dashboard update after local event|p95 <= 250 ms|Local websocket/SSE to browser.|
|NFR-LAT-005: Market-data processing after WebSocket receipt|p95 <= 10 ms per message batch|Core daemon only; no LLM.|
|NFR-LAT-006: X social signal after API delivery|p95 <= 500 ms local processing|End-to-end limited by X delivery latency; Filtered Stream documents P99 seconds-level latency [S13].|
|NFR-LAT-007: Agent reasoning loop|No millisecond target|LLM reasoning is asynchronous and must not block fast execution path.|

## 12.2 Reliability
- NFR-REL-001: The system SHALL persist campaign state before acknowledging any paper or live action.
- NFR-REL-002: The system SHALL recover from process restart without losing campaign ledger, positions, orders, fills, or risk state.
- NFR-REL-003: The system SHALL mark all markets stale if WebSocket connectivity is lost beyond a configurable threshold.
- NFR-REL-004: The system SHALL support safe degradation: if X fails, market data and paper trading can continue only if configured to allow non-social trades.
- NFR-REL-005: The system SHALL maintain idempotency keys for trade intents, risk decisions, order submissions, and cancellations.

## 12.3 Security
- NFR-SEC-001: Secrets SHALL be stored in an OS keychain, encrypted vault, hardware wallet, or equivalent isolated secret store.
- NFR-SEC-002: Private keys, API secrets, bearer tokens, and seed phrases SHALL never be returned by any MCP tool, dashboard endpoint, log, or audit export.
- NFR-SEC-003: All MCP tools with side effects SHALL require explicit schemas and reject unknown fields.
- NFR-SEC-004: Untrusted inputs from markets, X posts, comments, or websites SHALL be tagged as untrusted and sanitized before LLM exposure.
- NFR-SEC-005: Prompt-injection red-team tests SHALL be required before live adapter activation.
- NFR-SEC-006: The dashboard SHALL require a local access token if exposed beyond localhost.
- NFR-SEC-007: The live adapter SHALL be a separate process with the smallest possible API surface.

## 12.4 Privacy and Data Governance
- NFR-PRIV-001: The system SHALL keep all campaign data local by default.
- NFR-PRIV-002: External LLM calls, if used, SHALL be explicitly configured and visible in the dashboard.
- NFR-PRIV-003: Social data storage SHALL respect X API policy, deletion requirements, display requirements, and rate limits [S15].
- NFR-PRIV-004: The audit export SHALL support redaction of secrets, auth headers, tokens, and personal identifiers not required for analysis.

## 12.5 Observability
- NFR-OBS-001: Every tool call SHALL produce an audit event with timestamp, caller, input hash, output hash, latency, and result.
- NFR-OBS-002: Every trade intent SHALL be traceable to evidence references, market data snapshots, risk policy version, and agent prompt version.
- NFR-OBS-003: The system SHALL expose Prometheus-compatible metrics or a similar local metrics endpoint.
- NFR-OBS-004: The system SHALL track market-data lag, API throttling, WebSocket reconnects, X stream disconnects, risk rejections, fill simulation errors, and dashboard latency.

# 13. Data Model

|Entity|Key Fields|
|---|---|
|Market|market_id, event_id, condition_id, question, category, outcomes, token_ids, resolution_rules, end_time, enable_order_book, source_links|
|Token|token_id, market_id, outcome, best_bid, best_ask, last_trade_price, spread, depth, tick_size, stale_after|
|OrderBookSnapshot|snapshot_id, token_id, bids, asks, last_trade, sequence, received_at, source, checksum|
|Signal|signal_id, market_id, source_type, source_ref, text_summary, stance, confidence, novelty, timestamp, trust_score, policy_flags|
|TradeIntent|intent_id, campaign_id, market_id, token_id, side, price, max_size, thesis, evidence_refs, confidence, expires_at, created_by|
|RiskDecision|decision_id, intent_id, result, approved_price, approved_size, reasons, policy_version, created_at|
|Order|order_id, mode, intent_id, risk_decision_id, venue_ref, side, price, size, status, created_at, updated_at|
|Fill|fill_id, order_id, price, size, simulated_or_real, liquidity_source, snapshot_id, created_at|
|Position|position_id, campaign_id, market_id, token_id, size, avg_price, realized_pnl, unrealized_pnl, close_status|
|Campaign|campaign_id, mode, name, start_time, end_time, bankroll, market_filters, risk_limits, status, dashboard_url|
|Lesson|lesson_id, campaign_id, trigger, observation, rule, confidence, valid_until, source_refs, memory_target|
|AuditEvent|event_id, type, actor, input_hash, output_hash, references, timestamp, latency_ms, previous_event_hash|

# 14. Risk Management Specification

## 14.1 Default Risk Limits

|Limit|Default|Reason|
|---|---|---|
|Max single trade risk|1% of paper bankroll|Avoid overfitting and large losses from one thesis.|
|Max exposure per market|5% of paper bankroll|Avoid concentration in one resolution event.|
|Max category exposure|15% of paper bankroll|Avoid correlated topic risk.|
|Daily loss stop|5% of paper bankroll|Force review after drawdown.|
|Campaign loss stop|10% of paper bankroll|Terminate failed campaign early.|
|Minimum order-book depth|Configurable by market; default rejects thin books|Reduce fake paper fills.|
|Maximum spread|Configurable; default rejects wide spreads|Avoid paying extreme execution cost.|
|Minimum evidence count|At least one primary/official source or two independent secondary sources|Prevent trades based on unsourced social noise.|
|Minimum thesis/counter-thesis|Both required|Force adversarial thinking.|

## 14.2 Prohibited Behaviors
- No martingale or automatic doubling after losses.
- No hidden leverage or synthetic leverage in MVP.
- No trade based only on a viral post without source verification.
- No trade when market resolution rules are not understood.
- No trade if data staleness exceeds threshold.
- No new orders after emergency stop until manual reset.
- No live orders while geoblock or age/jurisdiction checks are unresolved.

## 14.3 Risk Decision Format

```text
RiskDecision {
  decision_id: string
  intent_id: string
  result: approve | modify | reject
  approved_size_usd: number | null
  approved_limit_price: number | null
  reasons: string[]
  violated_rules: string[]
  policy_version: string
  data_freshness_ms: number
  exposure_after_trade: object
  required_user_confirmations: string[]
}
```

# 15. Campaign Evaluation and Promotion

## 15.1 Paper Campaign Metrics

|Metric|Definition|Why It Matters|
|---|---|---|
|Net P&L|Realized + unrealized paper profit/loss after conservative costs.|Basic performance.|
|Max drawdown|Largest peak-to-trough paper equity drop.|Risk of ruin proxy.|
|Hit rate|Percentage of profitable closed positions.|Useful but not sufficient.|
|Profit factor|Gross wins divided by gross losses.|Quality of wins vs losses.|
|Brier score / calibration|Measures whether predicted probabilities match outcomes.|Important for prediction markets.|
|Market baseline comparison|Compare agent forecast to market-implied probability.|Detect whether agent adds information.|
|Slippage model error|Difference between expected and simulated execution.|Detect fake paper profitability.|
|Risk rejections|Rejected/modified intents and reasons.|Shows whether agent is trying unsafe trades.|
|Source reliability|How often sources were stale, wrong, or irrelevant.|Detect poor data pipeline.|
|Decision sample size|Number of independent decisions and markets.|Avoid lucky short runs.|

## 15.2 Promotion Criteria

The promotion report SHALL not automatically unlock live mode. It may only recommend eligibility review. A 1-3 day campaign can reveal bugs, latency issues, risk problems, and obvious bad strategy, but it is not enough to prove a durable edge. The report must state sample-size limitations clearly.
- PC-001: No live recommendation if operator eligibility, age, jurisdiction, geoblock, or platform terms checks fail.
- PC-002: No live recommendation if there were unresolved data outages, WebSocket desyncs, fill-simulation defects, or audit gaps.
- PC-003: No live recommendation if paper P&L is positive only because of optimistic fill assumptions.
- PC-004: No live recommendation unless drawdown, exposure, and risk rejections stayed within configured limits.
- PC-005: No live recommendation if the agent repeatedly tried to bypass risk rules or ignored counterevidence.
- PC-006: A positive paper run SHALL be treated as evidence for more testing, not as proof of profit.

## 15.3 Promotion Report Structure
1. Campaign summary: duration, market universe, bankroll, risk profile, number of intents, number of fills.
1. Performance summary: P&L, drawdown, exposure, calibration, baseline comparison.
1. Execution quality: order-book depth, slippage, partial fills, stale-data events.
1. Risk quality: rejections, rule violations, near misses, emergency stops.
1. Source quality: strongest sources, weakest sources, stale evidence, false social signals.
1. Learning summary: lessons written, skill candidates, repeated failure patterns.
1. Compliance status: age/jurisdiction/platform locks, live adapter state, required manual reviews.
1. Recommendation: continue paper, adjust strategy, reduce scope, or begin live eligibility review.

# 16. Agent Behavior Requirements

## 16.1 Required Agent Loop

```text
1. Select market universe under campaign constraints.
2. For each candidate market:
   a. Read market rules and resolution source.
   b. Check liquidity, spread, time to resolution, and data freshness.
   c. Gather external evidence and social summaries.
   d. Search past similar decisions and lessons.
   e. Form thesis and counter-thesis.
   f. Create trade intent only if evidence and risk conditions are sufficient.
3. Submit trade intent to deterministic risk check.
4. If approved, place paper order.
5. Monitor fill, position, invalidation criteria, and source updates.
6. Close/cancel according to thesis invalidation, risk stops, or campaign end.
7. Write postmortem and compact lesson.
8. Update dashboard and campaign report.
```

## 16.2 Subagent Rules
- Subagents MAY research separate markets, evidence sources, or postmortems in parallel.
- Subagents SHALL NOT receive live execution tools.
- Subagents SHALL NOT receive private keys, API secrets, or full wallet state.
- Subagents SHALL submit findings to a supervisor agent, which creates trade intents.
- The supervisor SHALL compare subagent outputs and request contradiction checks before large intents.

## 16.3 Agent Memory Rules
- The agent SHALL store compact lessons, not raw transcripts, in active Hermes memory.
- The agent SHALL use session search or the audit store for detailed historical recall.
- The agent SHALL not transform a lucky win into a permanent rule without repeated evidence.
- The agent SHALL remove or downgrade obsolete lessons when market structure, API behavior, or source quality changes.
- The agent SHALL tag lessons with source references and valid-until conditions.

# 17. Dashboard Requirements

## 17.1 Main Views

|View|Required Content|
|---|---|
|Campaign Overview|Status, time remaining, bankroll, P&L, drawdown, exposures, active limits.|
|Market Watchlist|Market rules, prices, spreads, volume, time to resolution, source status.|
|Agent Timeline|Agent thoughts summaries, tool calls, intents, risk checks, orders, fills, postmortems.|
|Trade Detail|Thesis, counter-thesis, evidence graph, order-book snapshot, risk decision, fill replay.|
|Source Intelligence|X summaries, official source updates, weather/sports/news adapters, source trust.|
|Risk Console|Limits, exposure clusters, rejections, lock states, emergency controls.|
|Learning Console|Lessons, repeated mistakes, skill candidates, memory exports.|
|Promotion Report|Final campaign evaluation and live-mode lock status.|

## 17.2 Dashboard UX Rules
- The dashboard SHALL make it impossible to confuse paper P&L with real P&L.
- Paper mode SHALL use visible “PAPER” labels in every portfolio, order, and fill view.
- Live mode, if ever enabled, SHALL use distinct confirmation panels and warning labels.
- Every chart or table SHALL be linked to underlying audit events.
- The dashboard SHALL show when data is stale, delayed, throttled, or simulated.

# 18. Compliance and Safety Requirements
- COMP-001: The system SHALL treat prediction-market trading as regulated or gambling-like activity depending on jurisdiction and SHALL not enable live mode without jurisdiction review.
- COMP-002: The system SHALL require age eligibility before live mode. Users below the legal age SHALL be limited to research and paper trading.
- COMP-003: The system SHALL check platform geographic restrictions before placing orders and SHALL not provide bypass instructions [S11].
- COMP-004: The system SHALL not use VPNs, proxies, or similar tools to circumvent platform or legal restrictions.
- COMP-005: The system SHALL maintain an operator acknowledgment that real trading involves substantial risk of loss.
- COMP-006: The system SHALL comply with X Developer Policy, automation rules, rate limits, and content handling requirements [S14][S15].
- COMP-007: The system SHALL prevent market manipulation, spam, coordinated misinformation, or automated posting designed to influence market outcomes.
- COMP-008: The system SHALL keep a permanent audit trail for live-eligible actions.

# 19. Testing Strategy

## 19.1 Test Types

|Test Type|Required Tests|
|---|---|
|Unit Tests|Risk rules, EV calculation, order-book normalization, source staleness, schema validation.|
|Integration Tests|Gamma API discovery, CLOB REST snapshots, WebSocket reconnects, X ingestion, dashboard updates.|
|Paper/Replay Tests|Historical replay, partial-fill simulation, slippage models, cancel behavior, stale data.|
|Security Tests|Prompt injection from market text and X posts, secret leakage tests, dashboard auth tests, MCP schema fuzzing.|
|Compliance Tests|Geoblock rejection, age/jurisdiction locks, live adapter disabled by default, confirmation tokens.|
|Chaos Tests|Network disconnects, API throttling, WebSocket desync, corrupted snapshots, process restart recovery.|
|User Acceptance Tests|Start campaign, watch dashboard, inspect trade, export report, emergency stop.|

## 19.2 Acceptance Criteria for MVP
- AC-001: A user can start a paper campaign from an MCP-capable agent and receive a local dashboard URL.
- AC-002: The system can discover and track order-book-enabled markets with live price updates.
- AC-003: The agent can create trade intents, but every intent must pass deterministic risk checks before paper execution.
- AC-004: Paper fills are auditable and replayable from stored snapshots.
- AC-005: The dashboard clearly shows paper P&L, risk state, agent rationales, evidence, and lessons.
- AC-006: The live adapter is disabled by default and cannot be triggered through raw MCP tool arguments.
- AC-007: Emergency stop freezes new actions and records an audit event.
- AC-008: The promotion report states whether the campaign is statistically weak, operationally safe, and compliance-eligible.

# 20. Suggested Implementation Architecture

The exact file layout is outside the SRS scope, but the following implementation architecture is recommended.

|Layer|Recommended Technology|Reason|
|---|---|---|
|Core daemon|Rust or Go|Predictable latency, safe concurrency, low memory overhead.|
|MCP server|TypeScript, Python, Rust, or Go|Should be thin; choose what integrates best with target agent.|
|Dashboard|TypeScript + local web server|Fast local UI and websocket/SSE updates.|
|Hot cache|In-process memory + optional Redis-compatible local store|Low-latency state reads.|
|Persistent store|SQLite for ledger/audit; DuckDB/Parquet for analytics|Local, simple, replayable.|
|Event bus|NATS, Redis Streams, or in-process channels for MVP|Separates data, execution, dashboard, and audit.|
|Secret store|OS keychain, encrypted vault, hardware wallet integration|Keeps keys out of MCP/LLM context.|
|Hermes integration|MCP resources/tools + lesson export files|Works with Hermes memory and skill system.|

## 20.1 Process Model

```text
process: prediction-core
  owns: market cache, risk engine, paper engine, audit writer

process: prediction-mcp
  owns: MCP protocol, schemas, prompts, resources
  calls: prediction-core local RPC only

process: prediction-dashboard
  owns: local UI, browser websocket, visualization
  reads: audit store and dashboard event stream

process: signal-ingestors
  owns: X API stream/search, weather/sports/news adapters
  writes: normalized Signal events to event bus

process: signing-vault (P2 / locked)
  owns: live signing and secret isolation
  accepts: approved order references only
```

# 21. Example Operator Workflows

## 21.1 Start a 48-Hour Paper Campaign

```text
Operator prompt to agent:
"Start a 48-hour PAPER campaign. Paper bankroll: 1,000 USD. Max single decision risk: 1%. Markets: liquid weather and sports only. Exclude politics and low-liquidity markets. Use official sources plus X summaries only for context. Show me the dashboard URL and do not enable live mode."

Expected agent actions:
1. Call start_paper_campaign.
2. Build watchlist using search_markets.
3. Open dashboard URL.
4. Research candidate markets.
5. Create trade intents only when evidence, liquidity, and risk checks pass.
6. Paper trade and write postmortems.
7. Produce promotion report at the end.
```

## 21.2 Review a Bad Trade

```text
Operator prompt to agent:
"Explain why paper trade PT-184 lost money. Show evidence, market snapshot, risk decision, and lesson."

Expected agent actions:
1. Fetch trade detail and audit events.
2. Replay order-book snapshots and fills.
3. Compare original thesis with final resolution path.
4. Identify failure mode.
5. Propose compact lesson or risk-rule adjustment.
```

## 21.3 Emergency Stop

```text
Operator action:
Click "Emergency Stop" in dashboard or ask the agent: "Emergency stop campaign now."

Expected system actions:
1. Freeze new trade intents.
2. Cancel open paper orders.
3. If live mode is active and legally allowed, attempt cancel-only actions.
4. Mark campaign as stopped.
5. Export emergency audit bundle.
```

# 22. Open Questions
- Which MCP-capable host is the first target: Hermes Agent, Codex, Kira, VS Code, Cursor, Claude Desktop, or a custom agent runner?
- Which X API access tier is available, and are lower-latency social feeds licensed?
- Which market categories are allowed in MVP: weather, sports, crypto, macro, politics, culture, or only low-controversy categories?
- What jurisdiction and age-verification process will be used for any future live-eligible mode?
- What fill model should be considered conservative enough for paper/live parity?
- Should strategy code be deterministic plugins, LLM-generated hypotheses, or a hybrid with locked deterministic templates?
- What exact Hermes memory provider should be used beyond built-in MEMORY.md and session search?

# 23. Final MVP Definition

The MVP is successful when an operator can connect an MCP-capable agent, start a paper campaign, observe all decisions in a local dashboard, inspect every trade with evidence and risk reasons, export a complete audit trail, and receive a sober promotion report. The MVP is not successful merely because paper P&L is positive. The system must prove that its data, fills, risk checks, and learning loop are reliable before live execution is even considered.

The live adapter remains a later-stage, compliance-gated module. The correct first build is a fast local paper-trading laboratory with agent supervision, not an uncontrolled money bot.
