"""OpenAI-compatible Chat Completions adapter (OpenRouter, etc.).

Speaks the widely-implemented ``/v1/chat/completions`` wire format via the
``openai`` SDK pointed at a custom ``base_url``, so any OpenAI-compatible
gateway works without a provider-specific SDK. The stock OpenAI provider keeps
its own adapter (``api_openai.py``) because it uses the proprietary Responses
API.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List

from .errors import ProviderApiError

try:
    import openai
except ImportError:
    openai = None

logger = logging.getLogger(__name__)


# Reasoning-capable models (Kimi K3 and others routed through OpenRouter)
# spend part of this same budget on an internal reasoning/thinking trace
# before ever writing the final answer -- observed in practice as a
# text-dense photo (lots to transcribe) burning the whole budget on
# reasoning alone, leaving `content=None` and finish_reason="length" with
# zero answer text produced. 4096 was too tight for that; this leaves
# enough room for a long reasoning trace and a full JSON answer.
MAX_TOKENS = 16384


def call_openai_compat_model(
    client: Any,
    model: str,
    content_items: List[Dict[str, str]],
    image_data_urls: List[str],
    *,
    dump_request: Callable[[Dict[str, Any]], None] | None = None,
) -> Any:
    """Call an OpenAI-compatible Chat Completions endpoint with data-URL images."""
    content: List[Dict[str, Any]] = []
    for item in content_items:
        if item.get("type") == "input_text" and isinstance(item.get("text"), str):
            content.append({"type": "text", "text": item["text"]})
    for url in image_data_urls:
        if not url:
            continue
        content.append({"type": "image_url", "image_url": {"url": url}})

    request_payload: Dict[str, Any] = {
        "model": model,
        "max_tokens": MAX_TOKENS,
        "temperature": 0,
        "messages": [{"role": "user", "content": content}],
    }
    if dump_request:
        dump_request(request_payload)

    logger.info("Starting analysis with model %s...", model)
    if openai is None:
        raise ProviderApiError("missing_dependency", "openai package is required for OpenAI-compatible providers.")

    tuned = client.with_options(timeout=180.0, max_retries=3)
    try:
        return tuned.chat.completions.create(**request_payload)
    except openai.RateLimitError as exc:
        raise ProviderApiError("rate_limit", str(exc), status_code=429) from exc
    except openai.BadRequestError as exc:
        raise ProviderApiError("invalid_input", str(exc), status_code=getattr(exc, "status_code", None)) from exc
    except openai.APIStatusError as exc:
        raise ProviderApiError("api_status", str(exc), status_code=getattr(exc, "status_code", None)) from exc


def extract_openai_compat_output_text(resp: Any) -> str:
    """Extract plain text from a Chat Completions response object."""
    choices = getattr(resp, "choices", None) or []
    text = ""
    finish_reason = None
    for choice in choices:
        finish_reason = getattr(choice, "finish_reason", None)
        message = getattr(choice, "message", None)
        content = getattr(message, "content", None)
        if isinstance(content, str) and content.strip():
            text = content
            break
        # Some gateways return content as a list of typed parts.
        if isinstance(content, list):
            parts = []
            for part in content:
                part_text = part.get("text") if isinstance(part, dict) else getattr(part, "text", None)
                if isinstance(part_text, str) and part_text.strip():
                    parts.append(part_text)
            if parts:
                text = "\n".join(parts)
                break

    if finish_reason == "length":
        # Must be checked before the "no text" fallback below: a response
        # truncated mid-reasoning (observed with Kimi K3 on text-dense
        # photos) has content=None and empty `text`, so without this check
        # first, the fallback would return str(resp) -- the raw Python
        # object repr, e.g. "ChatCompletion(id=..., reasoning='Let me
        # analyze...'" -- as if it were the model's answer, which then fails
        # JSON parsing downstream with a confusing, unrelated-looking error.
        raise ProviderApiError("length", "Model output was truncated by max_tokens.")
    if not text.strip():
        return str(resp)
    return text
