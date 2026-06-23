# photokin (core library)

LLM-based photo analysis: sends scanned photos (front/back) plus forwarded
metadata to a vision model and returns structured metadata (caption, keywords,
date/location guesses) as JSON, NDJSON streams, and changesets.

The core has **no ExifTool dependency**. ExifTool read/write lives in the
[`photokin/exiftool/`](exiftool/README.md) wrapper layer, which the CLI
composes on top of the core. Embedders who don't want ExifTool can call the
core directly.

## Module map

- `core.py` — orchestration: `analyze_photo`, `analyze_folder`,
  `analyze_manifest`, `process_manifest_stream` (streaming NDJSON + aggregate).
- `api.py` — provider dispatch (adapters imported lazily, so only the selected
  provider's SDK must be installed).
- `api_openai.py` / `api_claude.py` / `api_gemini.py` — provider adapters.
- `errors.py` — `ProviderApiError`, the normalized provider error.
- `utils.py` — `Config`, prompt assembly, image encoding, JSON repair/parse,
  filename grouping, manifest helpers.
- `merge.py` — merges model output with original metadata.
- `canonical.py` / `changeset.py` — canonical tag mapping and changeset NDJSON
  records (consumed by the ExifTool wrapper).
- `cli.py` — command-line interface; composes core + ExifTool wrapper.
- `public.py` — stable wrappers for embedding in other tools.
- `prompts_photo_ai/` — shared prompt files (used by all providers).

## Providers

| Provider | `--provider` / `LLM_PROVIDER` | Model setting | API key env |
|---|---|---|---|
| OpenAI (ChatGPT) | `openai` (default) | `--openai-model` / `OPENAI_MODEL` (default `gpt-4o`) | `OPENAI_API_KEY` |
| Anthropic (Claude) | `anthropic` (alias: `claude`) | `--claude-model` / `CLAUDE_MODEL`: `sonnet` or `haiku` alias, or full `claude-*` id | `ANTHROPIC_API_KEY` |
| Google (Gemini) | `gemini` (alias: `google`) | `--gemini-model` / `GEMINI_MODEL` (default `gemini-2.5-flash`) | `GEMINI_API_KEY` |

Claude aliases resolve via `CLAUDE_MODELS` in `utils.py`
(`sonnet` → `claude-sonnet-4-6`, `haiku` → `claude-haiku-4-5-20251001`;
default alias `sonnet`).

Orchestration resolves provider/model once (`utils.resolve_model_for_provider`)
and calls the shared dispatch in `api.py`. Prompt files are shared across
providers; the only provider-conditional prompt content is an extra JSON-syntax
guardrail appended for Gemini, plus Gemini-specific JSON repair in
`utils._cleanup_model_json`.

### Transport notes

- **OpenAI**: Responses API; `temperature=0` where the model supports it
  (omitted for o-series and gpt-5+). Archival `files.create` uploads run only
  for the OpenAI provider.
- **Claude**: Messages API, `max_tokens=4096`, `temperature=0`. Prompt parts
  are concatenated into one text block; images become base64 image blocks
  (MIME must be `image/jpeg`, `image/png`, `image/gif`, or `image/webp`),
  ordered images-first. `stop_reason=max_tokens` raises a `length` error.
- **Gemini**: `generate_content` with `response_mime_type=application/json`,
  `temperature=0`; images become `inline_data` parts.

## Error normalization

Provider exceptions are normalized to `ProviderApiError`
(`photokin.errors`) with stable `error_type` values:

- `rate_limit` — 429 / resource exhausted
- `overloaded` — Anthropic 529
- `invalid_input` — bad request / unusable input (all providers;
  `invalid_request` is a legacy alias still accepted downstream)
- `api_status` — other non-2xx from OpenAI/Anthropic
- `api_error` — other Gemini failures
- `length` — output truncated by `max_tokens`
- `content_filter` — Gemini blocked the response
- `missing_dependency` — provider selected but its SDK is not installed

Manifest-stream error payloads carry the normalized type/message and the HTTP
status code when available.

## Provenance keyword

After each response the effective model is read from `response.model`
(fallback: requested model) and keyword provenance is normalized to exactly
one marker: `<ProviderName> <ResolvedModelName> Analyzed`.

## Configuration

`utils.Config` (core fields only — ExifTool settings live in
`photokin.exiftool.ExiftoolConfig`):

- Provider: `provider`, `provider_name`, `model`, `claude_model_name`,
  `gemini_model_name`
- Prompts/vocab: `prompts_dir`, `vocab_path`, `forbidden_path`,
  `metadata_forward_path`, `no_update_vocab`, `fail_on_forbidden`
- Imaging: `jpeg_quality` (default 80), `max_edge` (default 1024)
- Thresholds: `date_confidence_threshold`, `location_confidence_threshold`,
  plus `date_override_*` policies used by `merge.py`
- Context: `photo_context_text`, `photo_context_file` (authoritative context
  forwarded to the model, capped at 200 KB)
- Debug: `debug_dump_llm_request`, `debug_dump_dir`, `run_batch_id`, `dry_run`

Environment defaults consumed by `Config`: `LLM_PROVIDER`,
`LLM_PROVIDER_NAME`, `OPENAI_MODEL`, `CLAUDE_MODEL`, `GEMINI_MODEL`. API keys
are read when building the provider client: `OPENAI_API_KEY`,
`ANTHROPIC_API_KEY`, `GEMINI_API_KEY`. Set `MEL_VERBOSE`/`MEL_DEBUG` for
extra warnings.

## CLI

```bash
# Single photo (dev/testing)
python -m photokin.cli photo.jpg --back photo_back.jpg --provider openai

# Folder of images
python -m photokin.cli --folder ./scans/ --provider anthropic --claude-model sonnet

# Manifest mode (recommended; used by the Lightroom plugin)
python -m photokin.cli --manifest batch.json \
  --output-file results.ndjson --changeset true \
  --exiftool-write true --exiftool-fields EXIF:UserComment
```

Key flags: `--provider`, `--openai-model`, `--claude-model`, `--gemini-model`,
`--update-policy {master_exact,merge_per_variant}`, `--max-edge`,
`--jpeg-quality`, `--process-all-variants`, `--photo-context-text/-file`,
`--output-sidecars`, `--batch-id`, `--changeset {true,false}` (manifest mode
only), `--dry-run`, `--debug-dump-llm-request {true,false}`,
`--debug-dump-dir`.

ExifTool pipeline flags (manifest mode; precedence **flag > env > default**):

- `--exiftool-write {true,false}` — apply changeset fields to files after
  analysis (env `EXIFTOOL_WRITE_ENABLED`, default true)
- `--exiftool-fields TAGS` — comma-separated tags to write (env
  `EXIFTOOL_FIELDS`, default `EXIF:UserComment`)
- `--exiftool-path PATH` — ExifTool binary (env `EXIFTOOL_PATH`, default
  auto-detect)

`--output-file` ending in `.ndjson` streams one record per finished photo;
`.json` writes a single aggregate object atomically.

## Embedding without ExifTool

`core.process_manifest_stream` accepts an optional
`metadata_hydrator: Callable[[list[dict]], None]` that runs on the manifest
items after loading and before grouping. The CLI passes
`photokin.exiftool.make_manifest_hydrator(...)` here; embedders can pass
their own callable or omit it entirely — the core itself never touches
ExifTool.

## Tests

```bash
cd python
python -m pytest photokin/tests
```
