# photokin (core library)

This is the developer's tour of the package: how a photo becomes metadata, and where each step of that journey lives in the code. Installation, CLI usage, flags, and ExifTool setup are all covered in the package README one level up — nothing here is required just to use the tool.

One architectural fact frames everything else: the core has no ExifTool dependency. ExifTool read/write lives in the `photokin/exiftool/` wrapper layer, and the CLI composes the two. Anyone embedding the core can skip the wrapper entirely, which is why it stays out.

## The pipeline

Input arrives as a single photo, a folder, or a manifest. All three become the same thing — a list of manifest items — and are grouped by one function, at a granularity `Config.group_by` selects: fronts matched with backs, variant scans folded together, album pages kept in order, crops recorded beside the scan they were cut from. Each group becomes one prompt: the shared prompt files, plus whatever metadata and photo context was forwarded with it. That prompt goes to exactly one provider adapter, and the response comes back as JSON — repaired if the model mangled it, which some reliably do. The parsed result is then merged against the metadata the photo already had, and leaves the pipeline as a result object, an NDJSON stream record, and optionally a changeset of proposed file writes for the ExifTool wrapper to apply.

That journey maps onto the files like this:

- `core.py` — orchestration: `analyze_photo`, `analyze_folder`, `analyze_manifest`, `process_manifest_stream` (streaming NDJSON + aggregate). Folder and single-photo input are translated into manifest items (`build_folder_manifest`, `build_single_photo_manifest`) and run through the stream, so there is one grouper and one batch loop; `build_manifest_buckets` is that grouper's entry point and takes the `group_by` value. Which analyzer a group reaches follows the group's own contents, not a flag: a group a single front/back pair fully describes takes `analyze_photo`, and one holding a page, a negative, a second front-side scan or a second back takes the group form and sends every file that owns a slot.
- `utils.py` — `Config`, prompt assembly, image encoding, JSON repair/parse, filename parsing (`parse_media_filename`) and folder listing (`list_folder_images`), manifest helpers.
- `face_utils.py` — normalizes face tags and renders them as the prompt block the model sees.
- `api.py` — provider dispatch (adapters imported lazily, so only the selected provider's SDK must be installed).
- `api_openai.py` / `api_claude.py` / `api_gemini.py` — provider adapters.
- `api_openai_compat.py` — generic OpenAI-compatible Chat Completions adapter (used by the OpenRouter provider; works with any compatible gateway).
- `errors.py` — `ProviderApiError`, the normalized provider error.
- `merge.py` — merges model output with original metadata.
- `canonical.py` / `changeset.py` — canonical tag mapping and changeset NDJSON records (consumed by the ExifTool wrapper).
- `cli.py` — command-line interface; composes core + ExifTool wrapper. One input token — positional, or through the `--folder`/`--manifest` aliases — is classified into a `ResolvedInput` and every kind then runs the same path, so `--output-file`, `--changeset` and the ExifTool write flags mean the same thing whatever was passed. It states its plan on stderr before the first model call, and `--dry-run` stops there.
- `cli_messages.py` — the wording of every user-facing CLI message, and the `RunPlan` summary. Pure and dependency-free, so the text is testable without importing the pipeline; `cli.py` decides when each one fires.
- `public.py` — stable wrappers for embedding in other tools.
- `prompts_photo_ai/` — shared prompt files (used by all providers).

## Grouping

Granularity is a single axis — `Config.group_by`, one of `utils.GROUP_BY_VALUES` — and `core.build_manifest_buckets` is what turns it into groups. Almost all of the difference between the three values is the key that function derives for each item:

- **`object`** (`GROUP_BY_OBJECT`, the default) keys on the entry's resolved group key — a manifest item's `group`/`base_id` when it gives one, else the `base_id` the filename grammar parses out. Every scan of one print — its front, its back, its variant letters, its pages, its negatives, its crops — lands in one bucket and shares one analysis.
- **`pair`** keys on that same group key plus the variant letter, so each rescan is judged on its own merits. The two halves are escaped before they are joined (`_escape_pair_half`, `_pair_bucket_key`); an explicit `group` is a free-form string, so an unescaped join let two unrelated objects spell one bucket and receive one caption, date and location between them.
- **`none`** keys on the file's own normalized path. It is the escape hatch for when filenames lie, and deliberately the most expensive and the lowest quality — a back analyzed alone is handwriting with no photo attached, and a multipage document becomes a set of unrelated pages.

An unrecognized value raises `ValueError` before the first group: argparse guards the CLI's `--group-by`, and nothing guards a library caller. `process_manifest_stream` reads the value once more for the one behavior that is not a key — under `none` it suppresses the orphan-crop warning, whose condition is true for every crop on every run once each file is its own group. Which analyzer a group reaches is decided by the group's own contents rather than by the axis: `analyze_photo` for anything a single front/back pair fully describes, the group form for a group holding a page, a negative, a second front-side scan or a second back.

**There is no primary scan any more.** Nothing gets analyzed on the group's behalf and has its answer copied onto its siblings. `utils.pick_master_index` is gone, `Config.process_all_variants` is replaced by `group_by`, and `update_policy` is dropped from `core.analyze_manifest`, `core.process_manifest_stream` and `public.analyze_manifest` along with the `UPDATE_MASTER_EXACT` / `UPDATE_MERGE_PER_VARIANT` constants — an embedder still passing that keyword gets a `TypeError` and wants `cfg.group_by` instead. What survives of the machinery is the manifest's `preferred` key, which no longer names the one analyzed file, there being no such file, but still breaks a tie between two files claiming the same `(version, part)` slot.

**Part markers.** One analysis per group means one set of keywords merged onto every file in it, so the two keywords that say *which part of the object a file is* have to be reasserted per file afterwards. `utils.PART_MARKER_KEYWORDS` is `{"back", "negative"}`; `core._item_part_marker` decides which one an item earns, and `utils.apply_part_keyword(record, marker, leaked)` appends that one and takes back the markers that leaked onto the record from its siblings. `leaked` is the set the group applied less the set the file itself carried before the merge (`utils.part_markers_in`, read off the pre-merge metadata), so the strip undoes the merge and nothing else: a print hand-tagged `Negative` in the user's catalog keeps that keyword even when a real negative sits beside it in the group. Both markers are kept out of the vocabulary file too, since the model starts proposing "Negative" the moment it is told a `Negative` part is present — approving it there would teach the model to emit a token this same code defines as not being a keyword.

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
- `api_status` — other non-2xx from OpenAI, Anthropic or OpenRouter
- `api_error` — other Gemini failures
- `length` — output truncated by `max_tokens`
- `content_filter` — Gemini blocked the response
- `missing_dependency` — provider selected but its SDK is not installed
- `missing_api_key` — provider selected but its API key env var is not set

Manifest-stream error payloads carry the normalized type/message and the HTTP status code when available. `missing_dependency` and `missing_api_key` are both raised eagerly in `core._build_provider_client`, before any request is attempted, so neither costs a request.

What happens next differs by mode, and it is worth knowing which you are in. Folder and single-photo mode ask the shared stream for their own failure contract (`process_manifest_stream(..., strict_run_failures=True)`): those two error types are treated as properties of the run rather than of one photo (`_RUN_FATAL_ERROR_TYPES`), so the first group to hit one aborts the batch with a single fatal error, and a run in which no group succeeded re-raises its first failure instead of returning an empty result. Manifest mode keeps the opposite default — it records one error entry per item and exits 0, so a manifest run with no API key produces a full set of `missing_api_key` records rather than one failure. Read the records, not just the exit status. The asymmetry was weighed in Phase C and kept: manifest mode is the Lightroom plugin's contract, and the plugin reads the per-item records, so failing the batch would tell it less than the records already do.

For `error_type` values whose message is already the full explanation (`SELF_EXPLANATORY_ERROR_TYPES` in `photokin.errors` — the two above, plus `rate_limit`, `overloaded`, `invalid_input`/`invalid_request`, `api_status`, `length`), both the CLI's top-level fatal error and manifest-stream per-item error records omit the traceback; anything else keeps it, since an unrecognized failure is exactly when a traceback earns its keep.

## When calls succeed

Every successful response gets one stamp on the way out: the effective model is read from `response.model` (falling back to the requested model), and keyword provenance is normalized to exactly one marker — `<ProviderName> <ResolvedModelName> Analyzed` — so you can always tell, years later, which model wrote a given analysis.

## Configuration

All the knobs mentioned above live on one dataclass, `utils.Config` (core fields only — ExifTool settings live in `photokin.exiftool.ExiftoolConfig`):

- Provider: `provider`, `provider_name`, `model`, `claude_model_name`, `gemini_model_name`, `openrouter_model_name`
- Prompts/vocab: `prompts_dir`, `vocab_path`, `forbidden_path`, `metadata_forward_path`, `no_update_vocab`, `fail_on_forbidden`
- Grouping: `group_by` (default `object`; see [Grouping](#grouping) above)
- Imaging: `jpeg_quality` (default 80), `max_edge` (default 1024)
- Thresholds: `date_confidence_threshold`, `location_confidence_threshold` (both default 0.7), plus `date_override_*` policies used by `merge.py`
- Context: `photo_context_text`, `photo_context_file` (authoritative context forwarded to the model, capped at 200 KB)
- Output: `pretty_json`
- Debug: `debug_dump_llm_request`, `debug_dump_dir`, `run_batch_id`, `dry_run`

Two of those are library-only, with no flag behind them. `pretty_json` indents the aggregate `.json` output and the stdout result document; the CLI never sets it, so both are compact unless an embedder asks otherwise (a generated manifest is always indented regardless, since it exists to be hand edited).

`dry_run` is the other, and it is not the CLI's `--dry-run`. That flag prints the plan summary and returns before the stream is entered, so there is no record for it to mark; the field exists for an embedder driving `process_manifest_stream` directly, where it stamps `dry_run: true` onto each NDJSON record of a rehearsal run. ExifTool's own preview — count the writes, perform none — is a third, separate thing, and lives on `ExiftoolConfig.dry_run`.

`Config` also picks up environment defaults so the CLI and embedders behave identically: `LLM_PROVIDER`, `LLM_PROVIDER_NAME`, `OPENAI_MODEL`, `CLAUDE_MODEL`, `GEMINI_MODEL`, `OPENROUTER_MODEL`. API keys are read when building the provider client: `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GEMINI_API_KEY`, `OPENROUTER_API_KEY`. Set `MEL_VERBOSE`/`MEL_DEBUG` for extra warnings.

## Embedding it yourself

If you're calling the library from your own code rather than the CLI, the seam is `core.process_manifest_stream`. Set `cfg.group_by` to one of `photokin.utils.GROUP_BY_VALUES` to choose the granularity; an unknown value raises `ValueError` before the first group. It returns `{"results": {path: record}, "errors": {path: payload}}` with one entry per file, the two disjoint — every file of a failed group carries that group's payload, bar one already recorded before the group raised part-way through — and it accepts an optional `metadata_hydrator: Callable[[list[dict]], None]` that runs on the manifest items after loading and before grouping. The CLI passes `photokin.exiftool.make_manifest_hydrator(...)` there; you can pass your own callable — pull existing metadata from a database, a sidecar format, anywhere — or omit it entirely. The core itself never touches ExifTool, which is the whole point of the seam.

## Tests

From the repository root:

```bash
python -m pytest photokin/tests tests
```
