"""Typed error hierarchy. Every error carries a machine-readable ``code`` so MCP
tool outputs and the dashboard can render structured, deterministic reasons."""

from __future__ import annotations


class HermesPMError(Exception):
    """Base error. ``code`` is a stable machine-readable token."""

    code = "error"

    def __init__(self, message: str, code: str | None = None, **details: object) -> None:
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code
        self.details = details

    def to_dict(self) -> dict[str, object]:
        return {"code": self.code, "message": self.message, "details": self.details}


class ConfigError(HermesPMError):
    code = "config_error"


class ValidationError(HermesPMError):
    code = "validation_error"


class SchemaRejectedError(ValidationError):
    """Raised when a tool input contains unknown fields or fails schema validation."""

    code = "schema_rejected"


class NotFoundError(HermesPMError):
    code = "not_found"


class StateError(HermesPMError):
    """Illegal state transition (e.g. acting on a stopped campaign)."""

    code = "state_error"


class RiskRejectedError(HermesPMError):
    code = "risk_rejected"


class StaleDataError(HermesPMError):
    code = "stale_data"


class ComplianceLockError(HermesPMError):
    """Raised when a live/compliance-gated action is attempted while locked."""

    code = "compliance_locked"


class EmergencyStopError(HermesPMError):
    code = "emergency_stop_active"


class UpstreamError(HermesPMError):
    code = "upstream_error"


class RateLimitedError(UpstreamError):
    code = "rate_limited"
