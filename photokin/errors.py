"""Normalized provider error shared by all LLM adapters."""

from __future__ import annotations


class ProviderApiError(RuntimeError):
    """Normalized provider API error for shared pipeline handling."""

    def __init__(self, error_type: str, message: str, *, status_code: int | None = None):
        super().__init__(message)
        self.error_type = error_type
        self.status_code = status_code


# error_type values whose message is already the full, actionable explanation
# -- a traceback adds noise, not information, so callers skip attaching one.
SELF_EXPLANATORY_ERROR_TYPES = frozenset(
    {
        "rate_limit",
        "overloaded",
        "invalid_input",
        "invalid_request",
        "api_status",
        "length",
        "missing_api_key",
        "missing_dependency",
    }
)
