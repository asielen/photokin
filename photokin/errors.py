"""Normalized provider error shared by all LLM adapters."""

from __future__ import annotations


class ProviderApiError(RuntimeError):
    """Normalized provider API error for shared pipeline handling."""

    def __init__(self, error_type: str, message: str, *, status_code: int | None = None):
        super().__init__(message)
        self.error_type = error_type
        self.status_code = status_code


def model_not_found_message(provider: str, model: str, flag: str, env_var: str) -> str:
    """Message for a model id the provider does not serve.

    The id often came from photokin's own pinned defaults rather than from the
    user, and providers retire and rename models over time, so the remedy names
    the per-run flag, the set-once env var, and the upgrade path for when the
    built-in default itself is the stale one.

    Args:
        provider: The provider's user-facing name.
        model: The model id the provider rejected.
        flag: The CLI flag that selects this provider's model.
        env_var: The environment variable behind that flag.

    Returns:
        The full model-not-found message.
    """
    return (
        f"{provider} does not serve the model '{model}' - it may have been retired or renamed.\n"
        f"pick a current model with {flag} or the {env_var} env var; if photokin's built-in "
        f"default is the stale one, upgrading photokin updates it."
    )


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
        "model_not_found",
    }
)
