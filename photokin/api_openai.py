"""OpenAI Responses API adapter."""

from __future__ import annotations

from typing import Any, Callable, Dict, List

from .errors import ProviderApiError

try:
    import openai
except ImportError:
    openai = None


def _model_supports_temperature(model: str) -> bool:
    """Return True if the OpenAI model supports the temperature parameter.

    GPT-4 and earlier models support temperature; GPT-5 and later do not.
    """
    name = model.lower()
    # o-series reasoning models and gpt-5+ do not support temperature
    if name.startswith("o") or name.startswith("gpt-5"):
        return False
    return True


def _request_responses_create(client: "openai.OpenAI", model: str, content_items: List[Dict[str, str]]) -> Any:
    tuned = client.with_options(timeout=180.0, max_retries=3)
    kwargs: Dict[str, Any] = {
        "model": model,
        "input": [{"role": "user", "content": content_items}],
    }
    if _model_supports_temperature(model):
        kwargs["temperature"] = 0
    return tuned.responses.create(**kwargs)


def call_openai_model(
    client: "openai.OpenAI",
    model: str,
    content_items: List[Dict[str, str]],
    image_data_urls: List[str],
    *,
    dump_request: Callable[[Dict[str, Any]], None] | None = None,
) -> Any:
    """Append data-URL image items and call OpenAI Responses API."""
    payload = list(content_items)
    for url in image_data_urls:
        if not url:
            continue
        payload.append({"type": "input_image", "image_url": url})

    request_payload: Dict[str, Any] = {
        "model": model,
        "input": [{"role": "user", "content": payload}],
    }
    if _model_supports_temperature(model):
        request_payload["temperature"] = 0
    if dump_request:
        dump_request(request_payload)

    print(f"[Run] Starting analysis with model {model}...")
    if openai is None:
        raise ProviderApiError("missing_dependency", "openai package is required for ChatGPT provider.")

    try:
        return _request_responses_create(client, model, payload)
    except openai.BadRequestError as exc:
        msg = str(exc)
        if "image_url" in msg and "got a string instead" in msg:
            fallback: List[Dict[str, Any]] = []
            for item in payload:
                if item.get("type") == "input_image" and isinstance(item.get("image_url"), str):
                    fallback.append({"type": "input_image", "image_url": {"url": item["image_url"]}})
                else:
                    fallback.append(item)
            if dump_request:
                fallback_payload: Dict[str, Any] = {
                    "model": model,
                    "input": [{"role": "user", "content": fallback}],
                }
                if _model_supports_temperature(model):
                    fallback_payload["temperature"] = 0
                dump_request(fallback_payload)
            try:
                return _request_responses_create(client, model, fallback)
            except openai.RateLimitError as exc2:
                raise ProviderApiError("rate_limit", str(exc2), status_code=429) from exc2
            except openai.APIStatusError as exc2:
                raise ProviderApiError("api_status", str(exc2), status_code=getattr(exc2, "status_code", None)) from exc2
        raise ProviderApiError("invalid_input", msg, status_code=getattr(exc, "status_code", None)) from exc
    except openai.RateLimitError as exc:
        raise ProviderApiError("rate_limit", str(exc), status_code=429) from exc
    except openai.APIStatusError as exc:
        raise ProviderApiError("api_status", str(exc), status_code=getattr(exc, "status_code", None)) from exc


def extract_openai_output_text(resp: Any) -> str:
    """Normalize OpenAI output object to a plain text response."""
    txt = getattr(resp, "output_text", None)
    if isinstance(txt, str) and txt.strip():
        return txt

    output = getattr(resp, "output", None)
    if output:
        for block in output:
            parts = getattr(block, "content", None)
            if not parts:
                continue
            for part in parts:
                if isinstance(part, dict) and part.get("text"):
                    return part["text"]
                part_text = getattr(part, "text", None)
                if isinstance(part_text, str) and part_text.strip():
                    return part_text

    model_dump_json = getattr(resp, "model_dump_json", None)
    if callable(model_dump_json):
        return model_dump_json()

    model_dump = getattr(resp, "model_dump", None)
    if callable(model_dump):
        return str(model_dump())
    return str(resp)
