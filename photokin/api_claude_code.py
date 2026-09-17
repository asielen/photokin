"""Claude Code CLI adapter: runs prompts through a local, subscription-authenticated
``claude`` binary instead of the Anthropic SDK.

Unlike the other adapters in this package, there is no HTTP client and no API
key -- ``client`` here is whatever :func:`photokin.core._build_provider_client`
built for the ``claude-code`` provider (a small object carrying the resolved
``claude`` binary path; see :func:`photokin.utils.claude_code_cli_status`).
Each call spawns ``claude -p`` as a subprocess, feeding it one stream-json
message on stdin and reading its stream-json events back off stdout.

Costs are billed against the operator's Claude subscription usage, not a
per-token API key, and the CLI's own rolling usage limits apply instead of
the Messages API's rate limits. See the "Claude Code CLI" section of
README.md before choosing this provider for a large batch run.
"""

from __future__ import annotations

import base64
import json
import logging
import mimetypes
import subprocess
from typing import Any, Callable, Dict, List
from urllib.parse import urlparse

from .errors import ProviderApiError

logger = logging.getLogger(__name__)

# Generous relative to a typical single-photo response, but still well short
# of a batch run silently hanging on one stuck subprocess -- mirrors the
# reasoning behind api_gemini's client-level timeout.
CLAUDE_CODE_TIMEOUT_SECONDS = 180

_SUPPORTED_MIMES = {"image/jpeg", "image/png", "image/gif", "image/webp"}


def _data_url_to_content_block(data_url: str) -> Dict[str, Any]:
    """Convert a data URL to the image content block the CLI's stream-json
    input expects -- the same shape as the Anthropic Messages API's image
    blocks (confirmed by direct trial: the CLI accepts this shape on stdin).
    """
    if not data_url.startswith("data:"):
        raise ProviderApiError("invalid_input", "Claude Code image input must be a data URL.")
    header, _, encoded = data_url.partition(",")
    if not encoded:
        raise ProviderApiError("invalid_input", "Claude Code image input data URL was empty.")

    mime = "image/jpeg"
    if ";base64" in header:
        parsed_mime = header[5:].split(";", 1)[0].strip()
        if parsed_mime:
            mime = parsed_mime

    if mime not in _SUPPORTED_MIMES:
        guessed, _ = mimetypes.guess_type(urlparse(data_url).path)
        if guessed in _SUPPORTED_MIMES:
            mime = guessed
        else:
            raise ProviderApiError("invalid_input", f"Unsupported image MIME type for Claude Code: {mime}")

    try:
        base64.b64decode(encoded, validate=True)
    except ValueError as exc:
        raise ProviderApiError("invalid_input", "Claude Code image input is not valid base64.") from exc

    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": mime,
            "data": encoded,
        },
    }


def _build_command(binary_path: str, model: str) -> List[str]:
    return [
        binary_path,
        "--bare",
        "-p",
        "--system-prompt",
        "",
        "--disallowed-tools",
        "*",
        "--model",
        model,
        "--input-format",
        "stream-json",
        "--output-format",
        "stream-json",
        # stream-json output is rejected without --verbose in --print mode.
        "--verbose",
    ]


def _classify_error_message(text: str) -> tuple[str, int | None]:
    """Best-effort (error_type, status_code) from the CLI's free-text result message.

    The CLI does not (as of this writing) expose a structured error taxonomy
    the way the Anthropic SDK's typed exceptions do -- only a human-readable
    ``result`` string. This maps the substrings observed in practice; refine
    as more failure modes are seen against a real logged-in CLI.
    """
    lowered = text.lower()
    if "auth" in lowered or "not logged in" in lowered or "login" in lowered:
        return "missing_api_key", 401
    if "rate limit" in lowered or "rate_limit" in lowered or "usage limit" in lowered:
        return "rate_limit", 429
    if "overloaded" in lowered:
        return "overloaded", 529
    return "api_status", None


def call_claude_code_model(
    client: Any,
    model: str,
    content_items: List[Dict[str, str]],
    image_data_urls: List[str],
    *,
    dump_request: Callable[[Dict[str, Any]], None] | None = None,
    thinking: bool = False,
) -> Any:
    """Run one prompt through the local ``claude`` CLI and return the parsed result event.

    ``thinking`` is accepted for interface parity with :func:`api_claude.call_claude_model`
    but is currently a no-op -- extended-thinking parity for this provider is
    tracked as follow-up work, not needed for the standard photo-analysis path.
    """
    binary_path = getattr(client, "binary_path", None) or "claude"

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
        content_blocks.append(_data_url_to_content_block(url))
    content_blocks.append({"type": "text", "text": combined_prompt})

    stdin_payload = {
        "type": "user",
        "message": {"role": "user", "content": content_blocks},
    }
    if dump_request:
        dump_request(stdin_payload)

    logger.info("Starting analysis with claude-code CLI (model %s)...", model)

    command = _build_command(binary_path, model)
    stdin_bytes = (json.dumps(stdin_payload) + "\n").encode("utf-8")

    try:
        proc = subprocess.run(
            command,
            input=stdin_bytes,
            capture_output=True,
            timeout=CLAUDE_CODE_TIMEOUT_SECONDS,
        )
    except FileNotFoundError as exc:
        raise ProviderApiError(
            "missing_dependency",
            f"Could not run the `claude` CLI at '{binary_path}'. Install Claude Code "
            "(https://claude.com/claude-code) and retry.",
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise ProviderApiError(
            "api_status",
            f"claude CLI did not respond within {CLAUDE_CODE_TIMEOUT_SECONDS}s.",
        ) from exc

    stdout_text = proc.stdout.decode("utf-8", errors="replace")
    result_event: Dict[str, Any] | None = None
    init_model: str | None = None
    for line in stdout_text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        if event.get("type") == "system" and event.get("subtype") == "init":
            candidate = event.get("model")
            if isinstance(candidate, str) and candidate.strip():
                init_model = candidate
        if event.get("type") == "result":
            result_event = event

    if result_event is None:
        stderr_text = proc.stderr.decode("utf-8", errors="replace").strip()
        raise ProviderApiError(
            "api_status",
            f"claude CLI produced no result (exit code {proc.returncode}). {stderr_text}".strip(),
        )

    if result_event.get("is_error"):
        message = result_event.get("result")
        message = message if isinstance(message, str) and message.strip() else "claude CLI reported an error."
        error_type, status_code = _classify_error_message(message)
        raise ProviderApiError(error_type, message, status_code=status_code)

    result_event.setdefault("model", init_model or model)
    return result_event


def extract_claude_code_output_text(resp: Any) -> str:
    """Extract plain text from a parsed claude-code CLI result event."""
    if isinstance(resp, dict):
        text = resp.get("result")
        if isinstance(text, str) and text.strip():
            return text.strip()
    return str(resp)
