"""Prometheus metrics: latency histograms for the NFR-LAT targets plus the
operational counters required by NFR-OBS-004 (market-data lag, throttling,
reconnects, rejections, fill-sim errors, dashboard latency)."""

from __future__ import annotations

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, generate_latest

_MS_BUCKETS = (1, 2, 5, 10, 25, 50, 100, 250, 500, 1000)


class Metrics:
    def __init__(self) -> None:
        self.reg = CollectorRegistry()
        self.tool_calls = Counter(
            "hpm_tool_calls_total", "MCP tool calls", ["tool", "status"], registry=self.reg
        )
        self.risk_decisions = Counter(
            "hpm_risk_decisions_total", "Risk decisions", ["result"], registry=self.reg
        )
        self.fills = Counter("hpm_paper_fills_total", "Paper fills", registry=self.reg)
        self.fill_sim_errors = Counter(
            "hpm_fill_sim_errors_total", "Fill simulation errors", registry=self.reg
        )
        self.ws_reconnects = Gauge("hpm_ws_reconnects", "WebSocket reconnects", registry=self.reg)
        self.api_throttles = Counter(
            "hpm_api_throttles_total", "Upstream rate-limit hits", registry=self.reg
        )
        self.x_stream_disconnects = Counter(
            "hpm_x_stream_disconnects_total", "X/social stream disconnects", registry=self.reg
        )
        self.stale_tokens = Gauge("hpm_stale_tokens", "Stale token count", registry=self.reg)
        self.market_data_lag_ms = Gauge(
            "hpm_market_data_lag_ms", "Age of newest market data", registry=self.reg
        )
        self.dashboard_push_latency_ms = Histogram(
            "hpm_dashboard_push_latency_ms", "Local event->dashboard push latency",
            buckets=_MS_BUCKETS, registry=self.reg,
        )
        self.lat_snapshot = Histogram(
            "hpm_latency_cached_snapshot_ms", "Cached snapshot latency",
            buckets=_MS_BUCKETS, registry=self.reg,
        )
        self.lat_risk = Histogram(
            "hpm_latency_risk_check_ms", "Risk check latency", buckets=_MS_BUCKETS, registry=self.reg
        )
        self.lat_order = Histogram(
            "hpm_latency_paper_order_ms", "Paper order acceptance latency",
            buckets=_MS_BUCKETS, registry=self.reg,
        )

    def render(self) -> bytes:
        return generate_latest(self.reg)
