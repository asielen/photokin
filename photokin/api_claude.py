"""Anthropic Claude Messages API adapter with normalized errors."""

from __future__ import annotations

import base64
import mimetypes
from typing import Any, Callable, Dict, List
from urllib.parse import urlparse

from .errors import ProviderApiError

try:
    import anthropic
except ImportError:
    anthropic = None


MAX_TOKENS = 4096
# Thinking shares the output budget with the answer, so give it headroom.
# 8192 was too tight for the judge scoring a large candidate roster in one
# response (an 8-way group comparison needs a full scored block per
# candidate plus notes) -- it was hitting this ceiling mid-thinking or
# mid-answer and either truncating or, worse, returning zero text blocks
# (extract_claude_output_text then falls back to the raw response repr,
# which parses as "unparseable" rather than a clear truncation error).
THINKING_MAX_TOKENS = 64000


def _thinking_params(model: str) -> Dict[str, Any]:
    """Request params enabling pre-answer reasoning for the given model."""
    if model.startswith("claude-haiku"):
        # Haiku does not support adaptive thinking; use a manual budget
        # (must be strictly less than max_tokens).
        return {"thinking": {"type": "enabled", "budget_tokens": 4096}, "max_tokens": THINKING_MAX_TOKENS}
    return {"thinking": {"type": "adaptive"}, "max_tokens": THINKING_MAX_TOKENS}


def _model_supports_temperature(model: str) -> bool:
    """Return True if the Claude model still accepts a `temperature` override.

    Sampling parameters (temperature/top_p/top_k) were removed starting with
    Opus 4.7, Sonnet 5, and the Fable/Mythos family -- sending temperature to
    one of those 400s with "`temperature` is deprecated for this model."
    Opus 4.6 and earlier, Sonnet 4.6 and earlier, Haiku, and legacy Claude 3.x
    models still accept it. Unrecognized/future model strings default to
    unsupported (temperature omitted) since the API is trending toward
    removing it everywhere, and a missing override is harmless while a
    rejected one fails the whole request.
    """
    m = model.lower()
    if m.startswith("claude-fable") or m.startswith("claude-mythos"):
        return False
    if m.startswith("claude-opus-4-6") or m.startswith("claude-opus-4-5") or \
       m.startswith("claude-opus-4-1") or m.startswith("claude-opus-4-0"):
        return True
    if m.startswith("claude-opus-4"):  # 4.7, 4.8, and any later Opus 4.x
        return False
    if m.startswith("claude-sonnet-5"):
        return False
    if m.startswith("claude-sonnet-4") or m.startswith("claude-haiku") or m.startswith("claude-3"):
        return True
    return False


def _data_url_to_image_block(data_url: str) -> Dict[str, Any]:
    if not data_url.startswith("data:"):
        raise ProviderApiError("invalid_input", "Claude image input must be a data URL.")
    header, _, encoded = data_url.partition(",")
    if not encoded:
        raise ProviderApiError("invalid_input", "Claude image input data URL was empty.")

    mime = "image/jpeg"
    if ";base64" in header:
        parsed_mime = header[5:].split(";", 1)[0].strip()
        if parsed_mime:
            mime = parsed_mime

    supported_mimes = {"image/jpeg", "image/png", "image/gif", "image/webp"}
    if mime not in supported_mimes:
        guessed, _ = mimetypes.guess_type(urlparse(data_url).path)
        if guessed in supported_mimes:
            mime = guessed
        else:
            raise ProviderApiError("invalid_input", f"Unsupported image MIME type for Claude: {mime}")

    try:
        base64.b64decode(encoded, validate=True)
    except ValueError as exc:
        raise ProviderApiError("invalid_input", "Claude image input is not valid base64.") from exc

    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": mime,
            "data": encoded,
        },
    }


def call_claude_model(
    client: Any,
    model: str,
    content_items: List[Dict[str, str]],
    image_data_urls: List[str],
    *,
    dump_request: Callable[[Dict[str, Any]], None] | None = None,
    thinking: bool = False,
) -> Any:
    """Call Anthropic Messages API with image-first user content ordering.

    ``thinking`` is off by default so the standard photo-analysis path is
    unchanged; opt in for tasks that benefit from pre-answer reasoning
    (e.g. model_compare's judge).
    """
    text_chunks = [
        item.get("text", "")
        for item in content_items
        if item.get("type") == "input_text" and isinstance(item.get("text"), str)
    ]
    combined_prompt = "\n\n".join(chunk.strip() for chunk in text_chunks if chunk.strip())

    content_blocks: List[Dict[str, Any]] = []
    for url in image_data_urls:
        if not url:
            continue
        content_blocks.append(_data_url_to_image_block(url))
    content_blocks.append({"type": "text", "text": combined_prompt})

    request_payload: Dict[str, Any] = {
        "model": model,
        "max_tokens": MAX_TOKENS,
        "messages": [{"role": "user", "content": content_blocks}],
    }
    if _model_supports_temperature(model):
        request_payload["temperature"] = 0
    if thinking:
        # The API rejects temperature overrides when thinking is enabled.
        request_payload.pop("temperature", None)
        request_payload.update(_thinking_params(model))
    if dump_request:
        dump_request(request_payload)

    print(f"[Run] Starting analysis with model {model}...")
    if anthropic is None:
        raise ProviderApiError("missing_dependency", "anthropic package is required for Claude provider.")

    try:
        # Always stream: the SDK refuses a plain create() outright once
        # max_tokens is large enough that it estimates the response could
        # take longer than 10 minutes to generate (independent of which
        # model is called) -- streaming avoids that guard entirely and
        # get_final_message() still returns the same Message shape
        # extract_claude_output_text() and the usage/cost code expect.
        with client.messages.stream(**request_payload) as stream:
            return stream.get_final_message()
    except anthropic.RateLimitError as exc:
        raise ProviderApiError("rate_limit", str(exc), status_code=429) from exc
    except anthropic.BadRequestError as exc:
        raise ProviderApiError("invalid_input", str(exc), status_code=getattr(exc, "status_code", None)) from exc
    except anthropic.APIStatusError as exc:
        err_type = "overloaded" if getattr(exc, "status_code", None) == 529 else "api_status"
        raise ProviderApiError(err_type, str(exc), status_code=getattr(exc, "status_code", None)) from exc


def extract_claude_output_text(resp: Any) -> str:
    """Extract plain text from an Anthropic response object."""
    blocks = getattr(resp, "content", None) or []
    parts: List[str] = []
    for block in blocks:
        block_type = getattr(block, "type", None)
        if block_type != "text":
            continue
        text = getattr(block, "text", None)
        if isinstance(text, str) and text.strip():
            parts.append(text)

    joined = "\n".join(parts).strip()
    if not joined:
        return str(resp)

    stop_reason = getattr(resp, "stop_reason", None)
    if stop_reason == "max_tokens":
        raise ProviderApiError("length", "Model output was truncated by max_tokens.")
    return joined
