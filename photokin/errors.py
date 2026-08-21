"""Normalized provider error shared by all LLM adapters."""

from __future__ import annotations


class ProviderApiError(RuntimeError):
    """Normalized provider API error for shared pipeline handling.

    Attributes:
        error_type: The normalized type (``rate_limit``, ``api_status``, ...).
        status_code: The HTTP status, when the failure came from one.
        provider_message: The provider's own message, extracted from the SDK
            exception's structured error body rather than read off ``str(exc)``
            -- which, for these SDKs, is often the whole body rendered as a
            Python dict repr (``Error code: 429 - {'type': 'error', ...}``).
            Fine for a human reading a log; useless for a caller that wants to
            show the provider's actual complaint without re-parsing that repr.
            ``None`` when nothing better than ``message`` was available.
        retry_after: Seconds to wait before retrying, read off the response's
            ``retry-after`` header when the SDK exposes one. ``None`` when the
            provider didn't send it or its shape isn't confirmed (Gemini, as
            of this writing -- see :mod:`photokin.api_gemini`).
    """

    def __init__(
        self,
        error_type: str,
        message: str,
        *,
        status_code: int | None = None,
        provider_message: str | None = None,
        retry_after: float | None = None,
    ):
        super().__init__(message)
        self.error_type = error_type
        self.status_code = status_code
        self.provider_message = provider_message
        self.retry_after = retry_after


def extract_provider_message(exc: Exception) -> str | None:
    """Best-effort clean message from an SDK exception's structured error body.

    Anthropic's and OpenAI's generated SDKs both expose ``exc.body`` -- the
    parsed JSON error response -- on their ``APIStatusError`` family, shaped
    ``{"error": {"message": "..."}}`` or ``{"type": "error", "error": {...}}``.
    This digs the nested message back out; returns ``None`` rather than
    guessing when the body isn't shaped that way (or isn't present at all,
    as for Gemini's exceptions).

    Args:
        exc: The SDK exception raised for a provider API call.

    Returns:
        The provider's own message, or ``None``.
    """
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict) and isinstance(error.get("message"), str):
            return error["message"]
        if isinstance(body.get("message"), str):
            return body["message"]
    return None


def extract_retry_after(exc: Exception) -> float | None:
    """Best-effort retry-after seconds from an httpx-backed SDK exception.

    Anthropic's and OpenAI's SDKs both carry the raw ``httpx.Response`` on
    ``exc.response``, so ``exc.response.headers`` works the same way for
    both. Returns ``None`` when there is no response, no header, or the
    header isn't a plain number (a provider may send an HTTP-date instead,
    which this does not attempt to parse).

    Args:
        exc: The SDK exception raised for a provider API call.

    Returns:
        The retry delay in seconds, or ``None``.
    """
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None)
    if headers is None:
        return None
    raw = headers.get("retry-after")
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


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
