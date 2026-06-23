"""Provider-dispatched LLM API helpers for OpenAI, Anthropic, and Gemini."""

from __future__ import annotations

from typing import Any, Callable, Dict, List

from . import utils
from .errors import ProviderApiError

# Provider adapters are imported lazily inside the dispatch functions so that
# only the selected provider's SDK needs to be installed.


def call_model(
    client: Any,
    model: str,
    content_items: List[Dict[str, str]],
    image_data_urls: List[str],
    *,
    provider: str = "openai",
    dump_request: Callable[[Dict[str, Any]], None] | None = None,
) -> Any:
    """Dispatch request transport to the configured provider adapter."""
    normalized_provider = utils.normalize_provider(provider)
    if normalized_provider == "anthropic":
        from .api_claude import call_claude_model

        return call_claude_model(
            client,
            model,
            content_items,
            image_data_urls,
            dump_request=dump_request,
        )
    if normalized_provider == "gemini":
        from .api_gemini import call_gemini_model

        return call_gemini_model(
            client,
            model,
            content_items,
            image_data_urls,
            dump_request=dump_request,
        )
    from .api_openai import call_openai_model

    return call_openai_model(
        client,
        model,
        content_items,
        image_data_urls,
        dump_request=dump_request,
    )


def extract_output_text(resp: Any, *, provider: str = "openai") -> str:
    """Extract provider response text into one normalized string."""
    normalized = utils.normalize_provider(provider)
    if normalized == "anthropic":
        from .api_claude import extract_claude_output_text

        return extract_claude_output_text(resp)
    if normalized == "gemini":
        from .api_gemini import extract_gemini_output_text

        return extract_gemini_output_text(resp)
    from .api_openai import extract_openai_output_text

    return extract_openai_output_text(resp)


def get_response_model(resp: Any, fallback_model: str) -> str:
    """Return resolved provider model string from response payload when present."""
    model = getattr(resp, "model", None)
    if isinstance(model, str) and model.strip():
        return model
    return fallback_model


__all__ = [
    "ProviderApiError",
    "call_model",
    "extract_output_text",
    "get_response_model",
]
