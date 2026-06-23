"""Google Gemini API adapter with normalized errors."""

from __future__ import annotations

import base64
import sys
from typing import Any, Callable, Dict, List

from .errors import ProviderApiError

try:
    from google.api_core import exceptions as google_api_exceptions
except ImportError:
    google_api_exceptions = None


def _normalize_gemini_model(model: str) -> str:
    """Ensure the model name is in the format expected by the google-genai SDK.

    The SDK's client.models.generate_content() expects the short model name
    (e.g. ``gemini-2.0-flash``) without a ``models/`` prefix.  Strip the
    prefix if it was accidentally included so the API receives a clean name.
    """
    stripped = model.strip()
    if stripped.startswith("models/"):
        stripped = stripped[len("models/"):]
    return stripped


def _data_url_to_gemini_part(data_url: str) -> Dict[str, Any]:
    """Convert a data URL to a Gemini inline_data part."""
    if not data_url.startswith("data:"):
        raise ProviderApiError("invalid_input", "Gemini image input must be a data URL.")
    header, _, encoded = data_url.partition(",")
    if not encoded:
        raise ProviderApiError("invalid_input", "Gemini image input data URL was empty.")

    mime = "image/jpeg"
    if ";base64" in header:
        parsed_mime = header[5:].split(";", 1)[0].strip()
        if parsed_mime:
            mime = parsed_mime

    try:
        base64.b64decode(encoded, validate=True)
    except ValueError as exc:
        raise ProviderApiError("invalid_input", "Gemini image input is not valid base64.") from exc

    return {"inline_data": {"mime_type": mime, "data": encoded}}


def call_gemini_model(
    client: Any,
    model: str,
    content_items: List[Dict[str, str]],
    image_data_urls: List[str],
    *,
    dump_request: Callable[[Dict[str, Any]], None] | None = None,
) -> Any:
    """Call Gemini generate_content API with image + text content."""
    model = _normalize_gemini_model(model)

    parts: List[Dict[str, Any]] = []
    for url in image_data_urls:
        if not url:
            continue
        parts.append(_data_url_to_gemini_part(url))

    text_chunks = [
        item.get("text", "")
        for item in content_items
        if item.get("type") == "input_text" and isinstance(item.get("text"), str)
    ]
    combined_prompt = "\n\n".join(chunk.strip() for chunk in text_chunks if chunk.strip())
    parts.append({"text": combined_prompt})

    api_config = {"temperature": 0, "response_mime_type": "application/json"}
    request_payload: Dict[str, Any] = {
        "model": model,
        "contents": [{"role": "user", "parts": parts}],
        "generation_config": api_config,
    }
    if dump_request:
        dump_request(request_payload)

    debug = dump_request is not None
    if debug:
        image_count = len([u for u in image_data_urls if u])
        print(
            f"[Debug] Gemini API call: model={model!r}, images={image_count},"
            f" config={api_config}",
            file=sys.stderr,
        )

    print(f"[Run] Starting analysis with model {model}...")

    try:
        return client.models.generate_content(
            model=model,
            contents=[{"role": "user", "parts": parts}],
            config=api_config,
        )
    except Exception as exc:
        status_code = getattr(exc, "code", None) or getattr(exc, "status_code", None)
        message = str(exc)

        if google_api_exceptions is not None:
            if isinstance(exc, google_api_exceptions.ResourceExhausted):
                raise ProviderApiError("rate_limit", message, status_code=429) from exc
            if isinstance(exc, google_api_exceptions.InvalidArgument):
                raise ProviderApiError("invalid_input", message, status_code=400) from exc

        if "429" in message or "RESOURCE_EXHAUSTED" in message:
            raise ProviderApiError("rate_limit", message, status_code=429) from exc
        if "400" in message or "INVALID_ARGUMENT" in message:
            raise ProviderApiError("invalid_input", message, status_code=400) from exc

        raise ProviderApiError("api_error", message, status_code=status_code) from exc


def extract_gemini_output_text(resp: Any) -> str:
    """Extract plain text from a Gemini response object."""
    text = getattr(resp, "text", None)
    if isinstance(text, str) and text.strip():
        return text

    candidates = getattr(resp, "candidates", None)
    if candidates:
        for candidate in candidates:
            content = getattr(candidate, "content", None)
            if not content:
                continue
            parts = getattr(content, "parts", None)
            if not parts:
                continue
            for part in parts:
                part_text = getattr(part, "text", None)
                if isinstance(part_text, str) and part_text.strip():
                    return part_text

    feedback = getattr(resp, "prompt_feedback", None)
    if feedback:
        block_reason = getattr(feedback, "block_reason", None)
        if block_reason:
            raise ProviderApiError("content_filter", f"Response blocked: {block_reason}")

    return ""
