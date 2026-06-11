"""Intelligence inputs: social (X) and external (weather/sports/news) adapters,
all behind a common contract with explicit source metadata (FR-EXT-002) and
mandatory sanitization of untrusted text (FR-SOC-003)."""

from hermes_pm.signals.base import AdapterMeta, SignalAdapter
from hermes_pm.signals.registry import SignalRegistry

__all__ = ["SignalAdapter", "AdapterMeta", "SignalRegistry"]
