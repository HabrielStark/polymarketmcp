"""Local operator dashboard (Section 17). FastAPI + WebSocket live updates with
mandatory PAPER labelling and visible staleness/lock indicators."""

from hermes_pm.dashboard.server import create_app, run_dashboard

__all__ = ["create_app", "run_dashboard"]
