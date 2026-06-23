"""Normalized provider error shared by all LLM adapters."""

from __future__ import annotations


class ProviderApiError(RuntimeError):
    """Normalized provider API error for shared pipeline handling."""

    def __init__(self, error_type: str, message: str, *, status_code: int | None = None):
        super().__init__(message)
        self.error_type = error_type
        self.status_code = status_code
