"""Claude Code CLI adapter: runs prompts through a local, subscription-authenticated
``claude`` binary instead of the Anthropic SDK.

EXPERIMENTAL: whether this actually bills against a Claude subscription's
included usage, versus metered API-rate usage credits, is disputed upstream
even for OAuth-authenticated ``claude -p`` calls -- see the "Claude Code CLI"
section of README.md before relying on this for a real batch run.

Unlike the other adapters in this package, there is no HTTP client and no API
key -- ``client`` here is whatever :func:`photokin.core._build_provider_client`
built for the ``claude-code`` provider (a small object carrying the resolved
``claude`` binary path; see :func:`photokin.utils.claude_code_cli_status`).
Each call spawns ``claude -p`` as a subprocess, feeding it one stream-json
message on stdin and reading its stream-json events back off stdout.

``--safe-mode`` (not ``--bare``) is what keeps this deterministic: ``--bare``
also strips OAuth/keychain auth (Claude Code then requires
``ANTHROPIC_API_KEY``, which this provider deliberately never sets, so every
call would fail authentication) -- confirmed by direct trial. ``--safe-mode``
disables the same CLAUDE.md/hooks/skills/MCP/plugins auto-discovery while
leaving auth alone.

Every subprocess this module spawns uses :func:`photokin.utils.sanitized_claude_code_env`,
not the raw process environment -- confirmed by direct trial that even
``--safe-mode`` prefers an inherited ``ANTHROPIC_API_KEY`` over OAuth login
(``apiKeySource`` flips from ``"none"`` to ``"ANTHROPIC_API_KEY"`` the moment
that variable is set), which would silently switch a user who also has
``--provider anthropic`` configured over to metered API billing here too.
"""

from __future__ import annotations

import json
import logging
import subprocess
from typing import Any, Callable, Dict, List

from .api_claude import _data_url_to_image_block as _data_url_to_content_block
from .errors import ProviderApiError
from .utils import sanitized_claude_code_env

logger = logging.getLogger(__name__)

# Generous relative to a typical single-photo response, but still well short
# of a batch run silently hanging on one stuck subprocess.
CLAUDE_CODE_TIMEOUT_SECONDS = 180


def _build_command(binary_path: str, model: str) -> List[str]:
    return [
        binary_path,
        # NOT --bare: --bare also disables OAuth/keychain auth (Claude Code
        # then requires ANTHROPIC_API_KEY, which this provider never sets),
        # so every call would fail with an authentication error -- confirmed
        # by direct trial. --safe-mode gives the same deterministic,
        # side-effect-free invocation (no CLAUDE.md/hooks/skills/MCP/plugins
        # auto-discovery) without touching how auth is resolved.
        "--safe-mode",
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
        # Print mode persists sessions to disk by default -- each one holding
        # the full prompt and base64 image data. A batch of thousands of
        # photos would otherwise leave that many sensitive-photo-containing
        # session files behind, contrary to this adapter's side-effect-free
        # design.
        "--no-session-persistence",
    ]


def _classify_error_message(text: str) -> tuple[str, int | None]:
    """Best-effort (error_type, status_code) from the CLI's free-text result message.

    The CLI does not (as of this writing) expose a structured error taxonomy
    the way the Anthropic SDK's typed exceptions do -- only a human-readable
    ``result`` string. This maps the substrings observed in practice; refine
    as more failure modes are seen against a real logged-in CLI.

    Deliberately narrow on the auth case: ``core._build_provider_client``'s
    preflight already confirmed ``claude auth status`` reports the CLI
    logged in before any call reaches here, so a broad ``"auth"`` substring
    match would treat a merely auth-flavored but likely transient failure --
    including the CLI's own generic "Authentication error ... may be a
    temporary network issue" message, seen in practice when auth is
    misconfigured in an unrelated way -- as a permanent, run-fatal
    credential problem (``missing_api_key`` is run-fatal; see
    ``core._RUN_FATAL_ERROR_TYPES``). Only an unambiguous "you are logged
    out" phrasing is classified that way; anything else auth-flavored falls
    through to the retryable default so one flaky response doesn't abort an
    otherwise-healthy batch.
    """
    lowered = text.lower()
    if "not logged in" in lowered or "please log in" in lowered or "invalid api key" in lowered:
        return "missing_api_key", 401
    if "rate limit" in lowered or "rate_limit" in lowered or "usage limit" in lowered:
        return "rate_limit", 429
    if "overloaded" in lowered:
        return "overloaded", 529
    if "model" in lowered and ("not found" in lowered or "does not exist" in lowered or "unknown model" in lowered):
        return "model_not_found", 404
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
            env=sanitized_claude_code_env(),
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
        # Checked ahead of the "no text" fallback below, mirroring
        # api_claude.extract_claude_output_text: a response truncated by the
        # token ceiling has no text yet, and falling through to str(resp)
        # there returns a dict repr that looks like model output to a
        # JSON-parsing caller and fails as an opaque JSONDecodeError instead
        # of this clear error.
        if resp.get("stop_reason") == "max_tokens":
            raise ProviderApiError("length", "Model output was truncated by max_tokens.")
        text = resp.get("result")
        if isinstance(text, str) and text.strip():
            return text.strip()
    return str(resp)
