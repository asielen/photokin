"""OpenAI Responses API adapter."""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List

from .errors import (
    ProviderApiError,
    extract_provider_message,
    extract_retry_after,
    model_not_found_message,
)

try:
    import openai
except ImportError:
    openai = None

logger = logging.getLogger(__name__)


def _model_supports_temperature(model: str) -> bool:
    """Return True if `model` is known in advance to support the temperature parameter.

    This is a fast-path guess only, based on naming patterns of reasoning-tier
    models observed so far (o-series, GPT-5+) that are known not to accept
    `temperature`. It intentionally does not need to stay exhaustive: a model
    this misses (e.g. a future GPT generation) still gets a correct request,
    just via one extra round trip -- see `_is_unsupported_temperature_error`
    and its retry in `call_openai_model`, which is the actual source of truth.
    """
    name = model.lower()
    # o-series reasoning models and gpt-5+ do not support temperature
    return not name.startswith(("o", "gpt-5"))


def _is_unsupported_temperature_error(message: str) -> bool:
    """True if an OpenAI 400 body is specifically rejecting `temperature`.

    Some reasoning-tier models reject this parameter but aren't caught by
    `_model_supports_temperature`'s naming heuristic (new model families,
    unexpected prefixes). Detecting the actual error lets `call_openai_model`
    retry without `temperature` regardless of the model name.
    """
    return "temperature" in message and "Unsupported parameter" in message


def _map_status_error(exc: Exception, model: str) -> ProviderApiError:
    """Map a retry-time OpenAI SDK exception to the matching ProviderApiError.

    Shared by every retry path in `call_openai_model` so each one maps
    rate-limit/not-found/status errors identically instead of repeating the
    same three branches per retry site.
    """
    if isinstance(exc, openai.RateLimitError):
        return ProviderApiError(
            "rate_limit",
            str(exc),
            status_code=429,
            provider_message=extract_provider_message(exc),
            retry_after=extract_retry_after(exc),
        )
    if isinstance(exc, openai.NotFoundError):
        # Same mapping as the outer NotFoundError handler below -- a retry
        # hits the API again with the same model id, so it can 404 on the
        # model just as the first attempt could have.
        return ProviderApiError(
            "model_not_found",
            model_not_found_message("OpenAI", model, "--openai-model", "OPENAI_MODEL"),
            status_code=404,
        )
    return ProviderApiError(
        "api_status",
        str(exc),
        status_code=getattr(exc, "status_code", None),
        provider_message=extract_provider_message(exc),
    )


def _request_responses_create(
    client: "openai.OpenAI",
    model: str,
    content_items: List[Dict[str, str]],
    *,
    force_no_temperature: bool = False,
) -> Any:
    tuned = client.with_options(timeout=180.0, max_retries=3)
    kwargs: Dict[str, Any] = {
        "model": model,
        "input": [{"role": "user", "content": content_items}],
    }
    if not force_no_temperature and _model_supports_temperature(model):
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

    logger.info("Starting analysis with model %s...", model)
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
            except (openai.RateLimitError, openai.NotFoundError, openai.APIStatusError) as exc2:
                raise _map_status_error(exc2, model) from exc2
        if _is_unsupported_temperature_error(msg):
            # A reasoning-tier model our naming heuristic didn't recognize
            # (see _model_supports_temperature) -- retry once with the
            # parameter dropped instead of failing every call for this model.
            if dump_request:
                retry_payload: Dict[str, Any] = {
                    "model": model,
                    "input": [{"role": "user", "content": payload}],
                }
                dump_request(retry_payload)
            try:
                return _request_responses_create(client, model, payload, force_no_temperature=True)
            except (openai.RateLimitError, openai.NotFoundError, openai.APIStatusError) as exc2:
                raise _map_status_error(exc2, model) from exc2
        raise ProviderApiError("invalid_input", msg, status_code=getattr(exc, "status_code", None)) from exc
    except (openai.RateLimitError, openai.NotFoundError, openai.APIStatusError) as exc:
        raise _map_status_error(exc, model) from exc


def extract_openai_output_text(resp: Any) -> str:
    """Normalize OpenAI output object to a plain text response.

    Unlike the other three providers, ``call_openai_model`` sets no
    ``max_output_tokens`` of its own -- omitting it uses the model's own
    (generous) default rather than a photokin-chosen ceiling low enough to
    realistically truncate, so there is no known problem here to size a
    budget around, and no photokin-set ceiling to retry higher against.
    This check stays purely defensive: if a model's own limit is ever hit
    regardless, it is still reported as a clear truncation rather than
    falling through to the raw-repr fallback below, unparseable JSON and
    all -- see api_claude.extract_claude_output_text for the bug that shape
    caused there.
    """
    if getattr(resp, "status", None) == "incomplete":
        incomplete_details = getattr(resp, "incomplete_details", None)
        if getattr(incomplete_details, "reason", None) == "max_output_tokens":
            raise ProviderApiError("length", "Model output was truncated by max_tokens.")

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
