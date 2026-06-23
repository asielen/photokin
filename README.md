# Python runtime for the MEL Lightroom plugin

This directory contains everything the Lua plugin shells out to, organized in
layers so the analysis engine can also be used outside Lightroom (directly
from the CLI or embedded in another tool).

## Layers

| Layer | Where | What it does |
|---|---|---|
| Core library | [`photo_archiver/`](photo_archiver/README.md) | LLM photo analysis: prompts, provider dispatch (OpenAI / Claude / Gemini), JSON parsing/repair, metadata merge, changeset emission. No ExifTool dependency. |
| ExifTool wrapper | [`photo_archiver/exiftool/`](photo_archiver/exiftool/README.md) | Optional-but-recommended ExifTool integration: reads metadata Lightroom can't supply (hydration) and writes changeset fields Lightroom can't write (apply). |
| Helper scripts | this directory | Standalone scripts used by specific plugin features (below). |
| Lua plugin | `Mel.lrplugin/*.lua` | Builds manifests, launches the CLI, tails NDJSON results, applies metadata via the Lightroom SDK. |

Dependency direction: the wrapper imports from the core; the core never
imports the wrapper. The CLI (`photo_archiver/cli.py`) composes the two into
the full pipeline: **hydrate → analyze → apply**.

## Helper scripts (top level)

- `mel_faces_xmp.py` — extracts Lightroom face regions from XMP (sidecar or
  embedded); called per-photo by the Lua side while building manifests.
- `face_utils.py` / `face_processor.py` — face-tag normalization and
  formatting helpers (LLM prompt blocks, captions, keywords).
- `face_tag_examples.py` — copy-paste recipes for custom face-tag workflows;
  not invoked by the plugin.
- `mel_exiftool_manifest.py` — standalone ExifTool→manifest reader; also
  reused by the wrapper layer for hydration.
- `add_caption_border.py` — adds a Polaroid-style caption border to exported
  JPEGs; called by the Polaroid export filter.

## Setup

Python 3.11+ recommended (3.10 needs `pip install toml` for TOML parsing).

```bash
cd Mel.lrplugin/python
pip install -r requirements.txt
```

Only the SDK for the provider you use is required at runtime (`openai`,
`anthropic`, or `google-genai`); the others can be omitted. ExifTool itself
must be installed separately (https://exiftool.org) or pointed to via
`--exiftool-path` / `EXIFTOOL_PATH`.

## How Lightroom invokes it

```bash
python -m photo_archiver.cli \
  --manifest BATCH_manifest.json \
  --output-file BATCH_results.ndjson \
  --changeset true \
  --exiftool-write true --exiftool-fields EXIF:UserComment
```

Provider selection and API keys are passed via environment variables
(`LLM_PROVIDER`, `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` / `GEMINI_API_KEY`,
model vars). See the [core README](photo_archiver/README.md) for the full CLI
and environment reference.

## Tests

```bash
cd Mel.lrplugin/python
python -m pytest
```

This runs both `photo_archiver/tests/` (core + wrapper) and `tests/`
(helper-script tests).
