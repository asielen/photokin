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
) -> Any:
    """Call Anthropic Messages API with image-first user content ordering."""
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
        "temperature": 0,
        "messages": [{"role": "user", "content": content_blocks}],
    }
    if dump_request:
        dump_request(request_payload)

    print(f"[Run] Starting analysis with model {model}...")
    if anthropic is None:
        raise ProviderApiError("missing_dependency", "anthropic package is required for Claude provider.")

    try:
        return client.messages.create(**request_payload)
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
