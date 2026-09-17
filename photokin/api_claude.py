"""Anthropic Claude Messages API adapter with normalized errors."""

from __future__ import annotations

import base64
import logging
import mimetypes
from typing import Any, Callable, Dict, List
from urllib.parse import urlparse

from .errors import (
    ProviderApiError,
    extract_provider_message,
    extract_retry_after,
    model_not_found_message,
)

try:
    import anthropic
except ImportError:
    anthropic = None

logger = logging.getLogger(__name__)


MAX_TOKENS = 16384
# Even without an explicit `thinking` request, newer models (observed on
# Sonnet 5) spend part of this budget on their own internal reasoning before
# ever writing an answer -- 4096 was too tight for a text-dense document and
# was hitting this ceiling mid-reasoning, before any text block existed. (It
# used to also surface as an opaque JSONDecodeError rather than a clear
# truncation -- extract_claude_output_text had a bug that let a response
# with zero text blocks skip the stop_reason check entirely; fixed there
# now.) call_claude_model retries once at THINKING_MAX_TOKENS when even this
# still truncates, so this only needs to comfortably cover the common case.
#
# Thinking shares the same output budget with the answer, so it needs more
# headroom still: 8192 was too tight for the judge scoring a large candidate
# roster in one response (an 8-way group comparison needs a full scored
# block per candidate plus notes).
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


def _is_unsupported_temperature_typeerror(message: str, payload: Dict[str, Any]) -> bool:
    """True if a ``TypeError`` from ``messages.stream(**payload)`` is about `temperature`.

    ``_model_supports_temperature``'s naming heuristic guesses from the model
    string alone, so it cannot know when an installed SDK version's generated
    client has dropped ``temperature`` from ``stream()``'s signature entirely
    -- that shows up as a client-side ``TypeError`` before any request is
    sent, not the server-side 400 the heuristic exists to avoid. Detecting it
    here lets the caller retry once with the parameter dropped instead of
    failing every request for that install.
    """
    return "temperature" in payload and "unexpected keyword argument 'temperature'" in message


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

    logger.info("Starting analysis with model %s...", model)
    if anthropic is None:
        raise ProviderApiError("missing_dependency", "anthropic package is required for Claude provider.")

    def _stream_and_finish(payload: Dict[str, Any]) -> Any:
        # Always stream: the SDK refuses a plain create() outright once
        # max_tokens is large enough that it estimates the response could
        # take longer than 10 minutes to generate (independent of which
        # model is called) -- streaming avoids that guard entirely and
        # get_final_message() still returns the same Message shape
        # extract_claude_output_text() and the usage/cost code expect.
        with client.messages.stream(**payload) as stream:
            return stream.get_final_message()

    def _call_with_temperature_retry(payload: Dict[str, Any]) -> Any:
        try:
            return _stream_and_finish(payload)
        except TypeError as exc:
            if not _is_unsupported_temperature_typeerror(str(exc), payload):
                raise
            retried = {k: v for k, v in payload.items() if k != "temperature"}
            if dump_request:
                dump_request(retried)
            return _stream_and_finish(retried)

    try:
        response = _call_with_temperature_retry(request_payload)
        if (
            getattr(response, "stop_reason", None) == "max_tokens"
            and request_payload["max_tokens"] < THINKING_MAX_TOKENS
        ):
            # Ran out of budget -- possibly mid-reasoning, before any answer
            # text existed (see extract_claude_output_text and MAX_TOKENS'
            # own comment). One retry at the same ceiling the thinking path
            # already uses, rather than fail a request that likely just
            # needed more room, since the common case never reaches here.
            escalated = {**request_payload, "max_tokens": THINKING_MAX_TOKENS}
            if dump_request:
                dump_request(escalated)
            response = _call_with_temperature_retry(escalated)
        return response
    except anthropic.RateLimitError as exc:
        raise ProviderApiError(
            "rate_limit",
            str(exc),
            status_code=429,
            provider_message=extract_provider_message(exc),
            retry_after=extract_retry_after(exc),
        ) from exc
    except anthropic.BadRequestError as exc:
        raise ProviderApiError("invalid_input", str(exc), status_code=getattr(exc, "status_code", None)) from exc
    except anthropic.NotFoundError as exc:
        # A 404 on the Messages API means the model id itself was rejected --
        # the id is usually photokin's pinned default, so say how to move on
        # from it rather than echoing the SDK's bare "not_found_error".
        raise ProviderApiError(
            "model_not_found",
            model_not_found_message("Anthropic", model, "--claude-model", "CLAUDE_MODEL"),
            status_code=404,
        ) from exc
    except anthropic.APIStatusError as exc:
        err_type = "overloaded" if getattr(exc, "status_code", None) == 529 else "api_status"
        raise ProviderApiError(
            err_type,
            str(exc),
            status_code=getattr(exc, "status_code", None),
            provider_message=extract_provider_message(exc),
        ) from exc


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
    # Checked ahead of the "no text" fallback below, not after it: a response
    # that ran out of budget while still thinking has no text block at all
    # yet (joined == "") -- str(resp) then looks like model output to
    # json.loads() and fails as an opaque JSONDecodeError instead of this
    # clear one. See MAX_TOKENS's comment above for the first time this bit.
    stop_reason = getattr(resp, "stop_reason", None)
    if stop_reason == "max_tokens":
        raise ProviderApiError("length", "Model output was truncated by max_tokens.")
    if not joined:
        return str(resp)
    return joined
