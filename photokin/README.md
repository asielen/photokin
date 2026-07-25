# photokin (core library)

This is the developer's tour of the package: how a photo becomes metadata, and where each step of that journey lives in the code. Installation, CLI usage, flags, and ExifTool setup are all covered in the package README one level up — nothing here is required just to use the tool.

One architectural fact frames everything else: the core has no ExifTool dependency. ExifTool read/write lives in the `photokin/exiftool/` wrapper layer, and the CLI composes the two. Anyone embedding the core can skip the wrapper entirely, which is why it stays out.

## The pipeline

Input arrives as a single photo, a folder, or a manifest, and is first grouped into physical objects — fronts matched with backs, variant scans folded together, album pages kept in order. Each group becomes one prompt: the shared prompt files, plus whatever metadata and photo context was forwarded with it. That prompt goes to exactly one provider adapter, and the response comes back as JSON — repaired if the model mangled it, which some reliably do. The parsed result is then merged against the metadata the photo already had, and leaves the pipeline as a result object, an NDJSON stream record, and optionally a changeset of proposed file writes for the ExifTool wrapper to apply.

That journey maps onto the files like this:

- `core.py` — orchestration: `analyze_photo`, `analyze_folder`, `analyze_manifest`, `process_manifest_stream` (streaming NDJSON + aggregate).
- `utils.py` — `Config`, prompt assembly, image encoding, JSON repair/parse, filename grouping, manifest helpers.
- `face_utils.py` — normalizes face tags and renders them as the prompt block the model sees.
- `api.py` — provider dispatch (adapters imported lazily, so only the selected provider's SDK must be installed).
- `api_openai.py` / `api_claude.py` / `api_gemini.py` — provider adapters.
- `api_openai_compat.py` — generic OpenAI-compatible Chat Completions adapter (used by the OpenRouter provider; works with any compatible gateway).
- `errors.py` — `ProviderApiError`, the normalized provider error.
- `merge.py` — merges model output with original metadata.
- `canonical.py` / `changeset.py` — canonical tag mapping and changeset NDJSON records (consumed by the ExifTool wrapper).
- `cli.py` — command-line interface; composes core + ExifTool wrapper.
- `public.py` — stable wrappers for embedding in other tools.
- `prompts_photo_ai/` — shared prompt files (used by all providers).

## Providers

The only step that changes per vendor is the model call itself — prompts, image encoding, and parsing are shared, and orchestration resolves the provider and model exactly once (`utils.resolve_model_for_provider`) before handing off to the dispatch in `api.py`:

| Provider | `--provider` / `LLM_PROVIDER` | Model setting | API key env |
|---|---|---|---|
| OpenAI (ChatGPT) | `openai` (default) | `--openai-model` / `OPENAI_MODEL` (default `gpt-4o`) | `OPENAI_API_KEY` |
| Anthropic (Claude) | `anthropic` (alias: `claude`) | `--claude-model` / `CLAUDE_MODEL`: `sonnet` or `haiku` alias, or full `claude-*` id via env | `ANTHROPIC_API_KEY` |
| Google (Gemini) | `gemini` (alias: `google`) | `--gemini-model` / `GEMINI_MODEL` (default `gemini-2.5-flash`) | `GEMINI_API_KEY` |
| OpenRouter (Kimi, Grok, Qwen, …) | `openrouter` | `--openrouter-model` / `OPENROUTER_MODEL` (default `moonshotai/kimi-k3`) | `OPENROUTER_API_KEY` |

Claude aliases resolve via `CLAUDE_MODELS` in `utils.py` (`sonnet` → `claude-sonnet-4-6`, `haiku` → `claude-haiku-4-5-20251001`; default alias `sonnet`). The one place prompt content does vary by provider: Gemini gets an extra JSON-syntax guardrail appended, paired with Gemini-specific JSON repair in `utils._cleanup_model_json` — it earned both.

"Shared everything upstream" still leaves real differences underneath, and they occasionally matter when debugging:

- **OpenAI**: Responses API; `temperature=0` where the model supports it (omitted for o-series and gpt-5+). Archival `files.create` uploads run only for the OpenAI provider.
- **Claude**: Messages API via streaming (the SDK refuses long non-streaming calls). `max_tokens=4096`, or a 64k output budget when extended thinking is enabled (adaptive thinking; Haiku gets a manual budget instead). `temperature=0` only on models that still accept it — Opus 4.7+, Sonnet 5, and the Fable/Mythos family reject the parameter outright. Prompt parts are concatenated into one text block; images become base64 image blocks (MIME must be `image/jpeg`, `image/png`, `image/gif`, or `image/webp`), ordered images-first. `stop_reason=max_tokens` raises a `length` error.
- **Gemini**: `generate_content` with `response_mime_type=application/json`, `temperature=0`; images become `inline_data` parts.
- **OpenRouter**: Chat Completions (`/v1/chat/completions`) via the `openai` SDK with `base_url=https://openrouter.ai/api/v1` (override with `OPENROUTER_BASE_URL` to point at any other OpenAI-compatible gateway). `max_tokens=16384` — reasoning models like Kimi spend part of the budget thinking before they write, and a smaller cap truncates them mid-answer. `temperature=0`; images become `image_url` data-URL parts, text-first. `finish_reason=length` raises a `length` error. Archival file uploads are skipped (OpenAI-only). One field note: a model page saying "multimodal" does not guarantee the endpoint OpenRouter routes you to accepts images — test a couple of photos before committing a big batch to an unfamiliar slug.

## When calls fail

Four vendors means four exception zoos, so every provider failure is normalized to a single `ProviderApiError` (`photokin.errors`) before it reaches calling code, with stable `error_type` values:

- `rate_limit` — 429 / resource exhausted
- `overloaded` — Anthropic 529
- `invalid_input` — bad request / unusable input (all providers; `invalid_request` is a legacy alias still accepted downstream)
- `api_status` — other non-2xx from OpenAI/Anthropic
- `api_error` — other Gemini failures
- `length` — output truncated by `max_tokens`
- `content_filter` — Gemini blocked the response
- `missing_dependency` — provider selected but its SDK is not installed

Manifest-stream error payloads carry the normalized type/message and the HTTP status code when available.

## When calls succeed

Every successful response gets one stamp on the way out: the effective model is read from `response.model` (falling back to the requested model), and keyword provenance is normalized to exactly one marker — `<ProviderName> <ResolvedModelName> Analyzed` — so you can always tell, years later, which model wrote a given analysis.

## Configuration

All the knobs mentioned above live on one dataclass, `utils.Config` (core fields only — ExifTool settings live in `photokin.exiftool.ExiftoolConfig`):

- Provider: `provider`, `provider_name`, `model`, `claude_model_name`, `gemini_model_name`, `openrouter_model_name`
- Prompts/vocab: `prompts_dir`, `vocab_path`, `forbidden_path`, `metadata_forward_path`, `no_update_vocab`, `fail_on_forbidden`
- Imaging: `jpeg_quality` (default 80), `max_edge` (default 1024)
- Thresholds: `date_confidence_threshold`, `location_confidence_threshold` (both default 0.7), plus `date_override_*` policies used by `merge.py`
- Context: `photo_context_text`, `photo_context_file` (authoritative context forwarded to the model, capped at 200 KB)
- Debug: `debug_dump_llm_request`, `debug_dump_dir`, `run_batch_id`, `dry_run`

`Config` also picks up environment defaults so the CLI and embedders behave identically: `LLM_PROVIDER`, `LLM_PROVIDER_NAME`, `OPENAI_MODEL`, `CLAUDE_MODEL`, `GEMINI_MODEL`, `OPENROUTER_MODEL`. API keys are read when building the provider client: `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GEMINI_API_KEY`, `OPENROUTER_API_KEY`. Set `MEL_VERBOSE`/`MEL_DEBUG` for extra warnings.

## Embedding it yourself

If you're calling the library from your own code rather than the CLI, the seam is `core.process_manifest_stream`: it accepts an optional `metadata_hydrator: Callable[[list[dict]], None]` that runs on the manifest items after loading and before grouping. The CLI passes `photokin.exiftool.make_manifest_hydrator(...)` there; you can pass your own callable — pull existing metadata from a database, a sidecar format, anywhere — or omit it entirely. The core itself never touches ExifTool, which is the whole point of the seam.

## Tests

```bash
cd python
python -m pytest photokin/tests
```
