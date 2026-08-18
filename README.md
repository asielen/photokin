# photokin

Run scanned photos and documents through a vision model and gets archival metadata back: a verbatim transcription of whatever is written on the front or back, a scene caption, keywords, and deliberately cautious date/location guesses - as JSON, NDJSON streams, or changesets that ExifTool can write into the files.

Compatible with OpenAI, Anthropic, Gemini, and OpenRouter API keys.

# Why should I use this?

I have inherited thousands of family photos and documents. While I would love to have the time to manually review each one, I realized that having an LLM do a first pass of them could help me manually review them later. So I started experimenting with the capabilities of LLMs and I was pleasantly surprised at the results. This library is my attempt to automate the process and get the key data I need from every photo and document to make my manual review easier.

## Quick start for a single photo

```bash
pip install "photokin[openai]"          # or [anthropic] / [gemini] / [all]
export OPENAI_API_KEY=sk-...            # setx on Windows

photokin scan_042.jpg --back scan_042_back.jpg
```

That's a good first command to confirm everything's wired up correctly: it only reads the images and prints one JSON document to your terminal, keyed by image path — one entry per file, so the back gets its own record — and nothing is written to `scan_042.jpg` or `scan_042_back.jpg` themselves. (Writing metadata into the files is a separate, explicit opt-in step; see [Setting up ExifTool](#setting-up-exiftool) below.) Abridged output:

```json
{
  "results": {
    "scan_042.jpg": {
      "keywords": ["Postcard", "1940s", "Military personnel", "..."],
      "caption": "[Back]\n27 november 44\nAlthough, I personally did not see this cathedral...",
      "ai_caption": "[AI Analysis]: A printed postcard showing... Inferred date: 1944-11-27 (confidence 0.95; evidence: handwritten date on back).",
      "category": "Postcard",
      "location_guess": {"country": "France", "city": "Le Mans", "confidence": 0.9},
      "date_guess": {"iso": "1944-11-27", "confidence": 0.95, "pattern": "Y!M!D!"}
    },
    "scan_042_back.jpg": { "keywords": ["...", "back"], "...": "..." }
  },
  "errors": {}
}
```

The transcription (`caption`) and the interpretation (`ai_caption`) are kept strictly separate — the model is not allowed to "improve" what's actually written on the object. That separation is most of the reason this tool exists.

The `back` in that second record's keywords is photokin's own, not the model's. Every file gets at most one keyword naming which part of the object it is: `back` on a reverse side, `negative` on a negative, and nothing on a front. They are the one thing not shared across a group — everything else in a group's analysis is — so you can always tell which file is which afterwards. See [Naming conventions](#folders-and-batches) for how a part is decided and for the rule that leaves a `back` or `Negative` you applied yourself exactly where it is.

New to Python, or starting from a completely bare machine? See the full [Windows Quick Start](#windows-quick-start) or [macOS Quick Start](#macos-quick-start) walkthroughs below.

## Windows Quick Start

This walks through a completely fresh Windows machine — nothing installed yet.

### 1. Install Python

Download Python 3.11 or newer from [python.org/downloads](https://www.python.org/downloads/). Photokin requires **Python 3.11+**.

On the first installer screen, check **"Add python.exe to PATH"** before clicking Install — this is the most common thing people miss, and without it `python` won't be recognized in a terminal.

Verify it worked by opening a **new** PowerShell window and running:

```powershell
python --version
```

### 2. Create a project folder

Pick a folder to hold your virtual environment and any manifest/output files. This does *not* need to contain your actual photos — you'll point photokin at wherever those already live.

```powershell
mkdir C:\Users\YourName\photokin-work
cd C:\Users\YourName\photokin-work
```

### 3. Create and activate a virtual environment

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
```

Your prompt should now start with `(.venv)`.

**Two common snags:**

- **Double-clicking `Activate.ps1` in File Explorer opens it in a text editor instead of running it.** This is expected — PowerShell scripts aren't meant to be launched by double-click. Always run it as a typed command from an open PowerShell window instead.
- **"Running scripts is disabled on this system" error.** PowerShell blocks script execution by default. Fix it once with:
  ```powershell
  Set-ExecutionPolicy RemoteSigned -Scope CurrentUser
  ```
  Confirm with `Y` when prompted, then re-run the activation command. If you'd rather not change the execution policy, use Command Prompt instead of PowerShell and run `.venv\Scripts\activate.bat`.

### 4. Install photokin

Install with the extra for whichever provider you're using:

```powershell
pip install "photokin[anthropic]"
```

(Swap `anthropic` for `openai`, `gemini`, or `all` as needed.)

### 5. Set your API key

For the current terminal session only:

```powershell
$env:ANTHROPIC_API_KEY = "sk-ant-..."
```

To make it persist across future terminal sessions:

```powershell
setx ANTHROPIC_API_KEY "sk-ant-..."
```

Note that `setx` doesn't affect your *current* window — open a new terminal to pick it up.

### 6. (Optional) Set up ExifTool

Only needed if you want photokin to read/write metadata into the image files themselves:

```powershell
python -m photokin.exiftool.fetch
```

This downloads the official ExifTool binary into `~/.photokin/bin` — no separate system install required.

### 7. Run it

```powershell
photokin scan_042.jpg --back scan_042_back.jpg --provider anthropic
```

or against a whole folder:

```powershell
photokin .\scans\ --provider anthropic > results.json
```

### Coming back later

Each new session, just reactivate the environment before running photokin:

```powershell
cd C:\Users\YourName\photokin-work
.venv\Scripts\Activate.ps1
photokin ...
```

## macOS Quick Start

This walks through a completely fresh Mac — nothing installed yet.

### 1. Install Python

Macs ship with an old system Python (and often no `python` command at all, only `python3`), so install a current one from [python.org/downloads](https://www.python.org/downloads/) — Photokin requires **Python 3.11+**. If you already use Homebrew, `brew install python@3.12` works just as well.

Verify it worked by opening a **new** Terminal window and running:

```bash
python3 --version
```

Use `python3` (not `python`) for every command below — that's normal on macOS, not a sign something's wrong.

### 2. Create a project folder

Pick a folder to hold your virtual environment and any manifest/output files. This does *not* need to contain your actual photos — you'll point photokin at wherever those already live.

```bash
mkdir ~/photokin-work
cd ~/photokin-work
```

### 3. Create and activate a virtual environment

```bash
python3 -m venv .venv
source .venv/bin/activate
```

Your prompt should now start with `(.venv)`.

**One common snag:** if `python3` isn't found anywhere, macOS may prompt you to install the Xcode Command Line Tools (a separate, smaller download triggered the first time a `python3`/`git`/etc. command runs). Either let it install, or just use the python.org installer from step 1, which doesn't depend on it.

### 4. Install photokin

Install with the extra for whichever provider you're using:

```bash
pip install "photokin[anthropic]"
```

(Swap `anthropic` for `openai`, `gemini`, or `all` as needed.)

### 5. Set your API key

For the current terminal session only:

```bash
export ANTHROPIC_API_KEY="sk-ant-..."
```

To make it persist across future terminal sessions, add that line to your shell's startup file — `~/.zshrc` on any Mac from the last several years (zsh is the default shell), or `~/.bash_profile` if you're on bash:

```bash
echo 'export ANTHROPIC_API_KEY="sk-ant-..."' >> ~/.zshrc
```

Open a new terminal window (or run `source ~/.zshrc`) to pick it up.

### 6. (Optional) Set up ExifTool

Only needed if you want photokin to read/write metadata into the image files themselves. Install it with Homebrew:

```bash
brew install exiftool
```

No Homebrew? Grab the installer package from exiftool.org instead.

### 7. Run it

```bash
photokin scan_042.jpg --back scan_042_back.jpg --provider anthropic
```

or against a whole folder:

```bash
photokin ./scans/ --provider anthropic > results.json
```

### Coming back later

Each new session, just reactivate the environment before running photokin:

```bash
cd ~/photokin-work
source .venv/bin/activate
photokin ...
```

## Folders and batches

Point it at a folder and it works through everything in it (non-recursive). Filename suffixes group scans of the same physical object automatically — `photo-a.jpg` / `photo-b.jpg` are variants, `photo-back.jpg` is the reverse side, `album-page1.jpg` / `album-page2.jpg` are pages of one document — and each group is analyzed together as one object.

Folder and manifest input are read by the same grouper, so anything one handles the other handles identically. A set with no plain front scan in it is analyzed like any other object — nothing but pages (`album-page1.jpg`, `album-page2.jpg`), nothing but a negative, or nothing but a back (`box3_030-back.jpg`, where the front was never scanned or lives in another folder). Every page of a document goes to the model in one call, and every scan of a print goes in one call with its siblings. Albums, multi-page documents, negative-only scans and loose backs no longer need a manifest. (Earlier releases grouped those sets correctly and then skipped them, warning per group; that limitation is gone.)

**How much is one object: `--group-by {object,pair,none}`.** Granularity is a single axis, defaulting to `object`. On `box3_025.jpg`, `box3_025-back.jpg`, `box3_025b.jpg`, `box3_025b-back.jpg`, `box3_025c.jpg`:

| Value | Group key | On that five-file set |
|---|---|---|
| `object` (default) | the print | 1 call, 5 images; every scan shares one analysis |
| `pair` | the print plus the variant letter | 3 calls, 5 images; each rescan judged on its own merits |
| `none` | the file | 5 calls, 5 images; every file alone, backs separated from fronts |

`object` is the default because scans of one print are one print: a shared date and location is the wanted answer, not three opinions to reconcile. `pair` costs one call per rescan and gives each its own verdict; on ordinary input — a group with no variant letters at all — it is identical to `object`, group id included. `none` is an **escape hatch for when filenames lie**, not a normal mode. It is the most expensive and the lowest quality: a back analyzed alone is handwriting with no photo attached, caption, date and location inference all lean on seeing the front, a multipage document is split into unrelated pages, and every crop becomes its own object and is analyzed as one. Reach for it when the grammar has mis-grouped something and you want the files judged individually; not otherwise.

> SIDE NOTE ON EXPECTED **Naming conventions.** The full suffix grammar is `name[letter][-front|-back|-negative|-pageN][-crop]`, case-insensitive, applied right to left:
>
> | Example | Meaning |
> |---|---|
> | `box3_025.jpg` | the photo itself (the print's front, no variant letter) |
> | `box3_025-b.jpg` or `box3_025b.jpg` | another scan of the same object (variant letter, with or without dash after a digit) |
> | `box3_025-back.jpg` | the reverse side (`-front` and `-negative` work the same way) |
> | `album-page1.jpg`, `album-page2.jpg` | ordered pages of one document |
> | `box3_025-back-crop.jpg` | a cropped detail of its parent, recorded with the group but never analyzed as an object of its own — under `--group-by object` and `pair`; `--group-by none` has no groups, so every crop is analyzed as its own object and billed as one |
> | `box3_025.tif` beside `box3_025.jpg` | the same scan in two formats — the extension sits outside the grammar, so these are one object, not two photos |
>
> The variant letter comes before the part suffix (`025b-back-crop.jpg`), and a file with no explicit `-pageN` is only treated as page 1 if its group contains other explicitly numbered pages.
>
> **Same name, different extension — one object, one analysis.** A TIFF master kept beside the JPEG derivative made from it is how a scanning archive is normally filed, and photokin reads the pair the way you mean it: the extension is not part of the name, so `box3_025.tif` and `box3_025.jpg` are one scan of one print and claim one place in the group. One of the two is sent to the model and the analysis is written to **both**, so the pair costs one call and one image where two unrelated photos would cost two of each. It applies to every side and every variant alike — `box3_025-back.tif` and `box3_025-back.jpg` pair up the same way.
>
> This is a cost saving, not a loss. Both files come back with a full record, the keywords, caption, date and location the model produced, and any metadata write you asked for. The run still tells you which of the two it did not upload: a warning names it, `all_variant_files.displaced` lists it, and it is counted in the closing `N file(s) recorded without being sent to the model` line. So a folder of 200 TIFF/JPEG pairs ends by reporting 200 — one per pair — with all 400 files recorded and 200 images uploaded instead of 400. That number is the saving, counted; it is not a warning that anything went missing.
>
> Which one is sent is the higher-fidelity one, and it is the same on every run: TIFF first, then PNG, then the lossy formats, with the path settling anything still tied. The master goes to the model rather than the export, because only one of the pair is read and JPEG artifacts are exactly what costs you a line of faint pencil on the back of a card. If you want the other one analyzed anyway, mark it `"preferred": true` in a manifest; that outranks the format.
>
> Every input mode reads this whole grammar and resolves it the same way, because they all route through one grouper. Pages and negatives reach the model in folder mode exactly as they do in manifest mode; crops are recorded with their group rather than analyzed, with a warning naming each one. To see how a folder would be grouped before spending anything on it, run `photokin ./scans/ --generate-manifest scans-manifest.json`: it writes the manifest the run would have used and stops.
>
> Resolution does not depend on the order the files are listed in: a crop never takes its parent's place, and a negative is analyzed as a negative rather than mistaken for the front. Both are recorded in the group's `all_variant_files` — under `crops` and `negatives` — and a negative travels under a `Negative` label and carries a `negative` keyword of its own, the way a back carries `back`. Those two keywords are per-file, so they are taken back off the *other* files of the group, which share its metadata. Only a marker the group itself applied is ever removed, and never from a file that already carried it before the merge — so a print you tagged `Negative` by hand keeps that keyword whether or not a real negative sits beside it in the group, and no removal is proposed against your catalog. Crops are named in a warning rather than sent to the model. The exception is a crop with no uncropped original for the same side of the same variant: with nothing else to stand for that side, the crop is analyzed in its place, and says so. That is judged per side, so a group holding `box3_025-crop.jpg` and `box3_025-back.jpg` still gets both a front and a back.
>
> Under `object` a group is sent every image it holds: given `box3_025.jpg`, `box3_025b.jpg` and `box3_025b-back.jpg`, all three go in the one call, because the variants are scans of one object and so that back is the object's back. That costs images rather than calls — one call per group either way — and only for groups holding more than one scan of a side, which are uncommon. It buys the model every scan of the print, so it can read detail off whichever came out clearest. A group that is one front, or one front and its own back, is sent exactly as it always was.
>
> Files that are recorded but not sent are exactly three kinds: a crop that yielded its parent's slot, the loser of two files claiming the same slot (the extension pair above), and a file displaced out of a slot something more specific already held. All three are named in a warning, listed in the record — crops under `all_variant_files.crops`, the other two under `all_variant_files.displaced` — and counted in the same closing number, so the summary can never read as clean while a warning says otherwise.

## Folder mode

```bash
photokin ./scans/ --provider anthropic > results.json
```

Folder mode prints one aggregate JSON to stdout, shaped `{"results": {...}, "errors": {...}}` with **one entry per file** — every image in the folder appears in exactly one of the two, backs, variant scans, album pages, negatives and crops included. A record names the whole group it belongs to under `all_variant_files`, so you can still tell which files were scanned together and which of them the model was shown. Per-group diagnostics, the plan summary and the closing summary go to stderr, so read both.

`--output-file` is not a manifest-only flag: a `.ndjson` path streams one record per finished photo (you can watch progress, and a crash doesn't lose completed work), while a `.json` path writes a single aggregate object atomically at the end — for a folder, a manifest or one photo alike. With it, stdout stays empty.

For bigger or more repeatable jobs a manifest is worth writing, and `--generate-manifest` turns a folder into exactly that file:

```bash
photokin ./scans/ --generate-manifest scans-manifest.json
```

It writes the manifest the folder run would have used — same files, same order — and exits without calling the model, so it costs nothing and doubles as a way to check the grouping before committing to a batch. Edit it (add `is_back`, `group`, existing `metadata`) and feed it straight back: `photokin scans-manifest.json`.

## Manifest mode

A manifest is a JSON file listing exactly what to process — an `items` array where each entry needs only a `path`. The sample below declares one physical object, a front scan and its back, and one line of batch-wide background context. Note the underscore in `box3_017_back.jpg`: the filename grammar reads only the hyphenated `-back`, so it is the `is_back` flag that folds the two files into one group and one model call rather than two unrelated photos.

batch.json:
```json
{
  "items": [
    {"path": "scans/box3_017.jpg"},
    {"path": "scans/box3_017_back.jpg", "is_back": true}
  ],
  "photo_context_text": "Church family photos, mostly New Jersey, 1930s-1950s."
}
```

```bash
photokin batch.json --output-file results.ndjson --changeset true
```

Flags are optional when the filename already says the same thing; they exist so files that don't follow the naming conventions can still be grouped correctly. An explicit flag always beats the filename, in both directions and including when the two contradict each other — anything else would leave the flag inert in exactly the situation it is there for. Every override that changes what the filename implied is logged, so a typo is visible rather than silent.

| Key | Effect |
|---|---|
| `is_back` | `true` marks the reverse side, `false` marks the front. `true` also repairs the group key by stripping a trailing `back` token, which is what puts `box3_017_back.jpg` in the same group as `box3_017.jpg`. |
| `is_crop` | `true` marks a cropped derivative, so the file is recorded with its group but not analyzed; `false` unmarks a file whose name ends in `-crop`. |
| `version` | The variant id, replacing any letter read off the filename. Any string, not just one letter; empty means no variant. |
| `group` | The group key outright, for names the grammar cannot parse at all. `base_id` is accepted as an alias and loses to `group` when both are given. |
| `preferred` | Breaks a tie between two files claiming the same slot — the same side of the same variant — so the one you name is the one sent and the other is recorded and warned about. It chooses between candidates; it cannot create a place for one. See below. |

`is_back` and `is_crop` may be written as JSON `true`/`false`, as `0`/`1`, or as the strings `"true"`, `"false"`, `"yes"`, `"no"`. A `null` value means "not specified" and leaves the filename in charge.

`preferred` is the exception and does not read that grammar: it is plain truthiness, so any non-empty string sets it and `"preferred": "false"` means **true**. Write it as a JSON `true`, or leave the key out entirely.

`preferred` used to pick the one file of a group that got analyzed. There is no longer one — under `object` every scan of the group is sent — so what survives is the narrower job above: deciding which of two files contesting one slot travels. It also still nominates the file the group's analysis is filed under.

Two shapes leave `preferred` with nothing to pick. A crop is a supporting view of its parent, so it yields the parent's place whenever the parent is listed — marking the crop `preferred` does not promote a derivative over the original it was cut from, and the crop is recorded rather than analyzed. Likewise a file that is untagged in a group whose front side is already claimed, such as a plain `album.jpg` beside an explicit `album-page1.jpg`: there is no part left for it to travel in, and `preferred` cannot make one. Both cases are warnings naming the file, and both are listed in the result record — crops under `all_variant_files.crops`, the rest under `all_variant_files.displaced` — so nothing disappears quietly.

## Existing metadata aware enrichment

Items may have existing `metadata` (face tags, existing captions and comments) that can be forwarded to the model as context. Additionally, you can supply `photo_context_text` as free-text additional context to a single photo or a folder. Such as "these photos are all part of a wedding album." The model treats it as truth for the whole batch. Both make a real difference on hard photos.

For auditing, `--changeset true` emits a changeset NDJSON alongside the results: a record of proposed field writes that the ExifTool wrapper can apply to the actual files, either in the same run (`-w`, or `--exiftool-write true --exiftool-fields EXIF:UserComment`) or later and separately. It lands in `dirname(--output-file or input)` and is named `<stem>_changeset.ndjson`, where the stem is the output file's own (minus a trailing `_results`) or, with no `--output-file`, the input's — so the `--output-file results.ndjson` run above writes `results_changeset.ndjson`, and `photokin ./scans/ --changeset true` writes `scans_changeset.ndjson` inside the folder:

```bash
python -m photokin.exiftool --changeset results_changeset.ndjson --enabled [--dry-run]
```

## API keys

Keys are plain environment variables, one per provider. Photokin reads them when it builds the provider client and nowhere else. They never end up in results, changesets, or debug dumps.

| Provider | Variable |
|---|---|
| OpenAI | `OPENAI_API_KEY` |
| Anthropic | `ANTHROPIC_API_KEY` |
| Gemini | `GEMINI_API_KEY` |
| OpenRouter | `OPENROUTER_API_KEY` |

For the current terminal session:

```bash
export OPENAI_API_KEY=sk-...            # macOS / Linux
$env:OPENAI_API_KEY = "sk-..."          # Windows PowerShell
```

To make it stick across sessions, add the `export` line to your shell profile (`~/.bashrc`, `~/.zshrc`), or on Windows run `setx OPENAI_API_KEY sk-...` once (takes effect in new terminals, not the current one). If you keep keys in a file, keep that file out of version control.

You only need the key for the provider you're actually calling. And since a batch run makes one paid API call per photo group, it's worth using a key with a spend cap set in the provider's dashboard — a typo in a folder path is a lot cheaper that way.

## Setting up ExifTool

ExifTool is optional, and can be both used for reading initial values from the photos (so you don't just overwrite everything) and also write to the photos after. Before analysis, it reads `EXIF:UserComment` straight out of the files so a comment already living in an image rides along to the model as context (hydration). After analysis, it writes approved changeset fields back into the files or their sidecars (apply).

Those two halves have different reach, which is worth knowing before you count on the first. Hydration runs for **manifest input only**, and only for items that already carry a `metadata` object whose `userComment` is missing or empty — a folder or single-photo run is never hydrated, because turning that on would change the prompt, the cost and the output of every existing folder run. Apply works for every input type.

Where each result field lands when written:

| Result field | Tag |
|---|---|
| `ai_caption` (the AI analysis) | `EXIF:UserComment` |
| `caption` (the verbatim transcription) | `XMP:dc:Description` |
| `keywords` | `XMP:dc:Subject` |
| `title` | `XMP:dc:Title` |
| `date_guess` (when confident enough) | `EXIF:DateTimeOriginal` |
| `location_guess` (when confident enough) | `IPTC:Country-PrimaryLocationName` / `Province-State` / `City` / `Sub-location` |

**Setup.** On Windows, run `python -m photokin.exiftool.fetch` once — it downloads the official ExifTool distribution from the project's SourceForge host and verifies it against the SHA256 exiftool.org publishes, into `~/.photokin/bin`, no system install needed. On macOS/Linux install it yourself: `brew install exiftool` or `apt install libimage-exiftool-perl`. At runtime the binary is found in this order: an explicit `--exiftool-path` / `EXIFTOOL_PATH`, then the downloaded copy in `~/.photokin/bin`, then whatever `exiftool` is on your `PATH`.

If none is found, hydration is skipped with a warning and the analysis still runs. A *requested write* is treated differently: the binary is resolved before the first model call, so `-w` with no ExifTool anywhere exits 2 immediately rather than analyzing the whole batch and only then discovering it cannot write any of it.

**Writing during a run.** Nothing is written into your files unless you ask for it: `--exiftool-write` defaults to `false`, and a changeset on its own only records what *would* be written. `-w` is the one-flag spelling of `--changeset true --exiftool-write true` — record the proposed writes and apply them — and works for folder, manifest and single-photo input alike. Spelled out, that is `--changeset true --exiftool-write true --exiftool-fields EXIF:UserComment`; `--exiftool-write true` is required rather than a confirmation of the default. The same settings are available as env vars (`EXIFTOOL_WRITE_ENABLED`, `EXIFTOOL_FIELDS`, `EXIFTOOL_PATH`), with flags winning over env over defaults. Every run prints a plan summary before its first model call naming the write set, so "nothing will be written" is visible up front; `--dry-run` prints that summary and stops.

**Writing later, with an audit step.** Since the changeset NDJSON is a plain record of proposed writes, you can inspect it first and apply it separately:

```bash
python -m photokin.exiftool --changeset results_changeset.ndjson --enabled --dry-run   # counts what would be written
python -m photokin.exiftool --changeset results_changeset.ndjson --enabled            # actually writes
```

The standalone applier also takes `--fields` to narrow which tags may be written, `--write-sidecar-only` to write `.xmp` sidecars instead of touching the originals, `--no-overwrite-original` to keep ExifTool's `_original` backup files, and `--output summary.json` for a machine-readable result. Date tags (`EXIF:DateTimeOriginal`, `EXIF:CreateDate`) are normalized to EXIF's `YYYY:MM:DD HH:MM:SS` format on the way in; unparseable dates become warnings, not writes.

## All flags

### Input modes

One input, given positionally; its type comes off the path. A directory is a
folder, a `.json` file is a manifest, an image file is a single photo — and the
run says which it decided on before it does anything else, so a mis-detection is
visible rather than surprising. The two aliases are still accepted and assert the
type instead of detecting it; passing a positional *and* an alias is an error.

| Flag                 | What it does |
|----------------------|---|
| `INPUT` (positional) | Folder of scans, `.json` manifest, or a single image; the type is detected from the path |
| `--back PATH`        | Back-side image, for single-photo input only |
| `--meta PATH`        | Original metadata JSON, for single-photo input only |
| `--folder DIR`       | Alias for a folder INPUT; asserts the path is a directory |
| `--manifest PATH`    | Alias for a manifest INPUT; asserts the path is a `.json` manifest file |

### Provider and model

| Flag                                              | What it does |
|---------------------------------------------------|---|
| `--provider {openai,anthropic,gemini,openrouter}` | Which backend to call (default `openai`) |
| `--openai-model NAME`                             | OpenAI model (default `gpt-4o`) |
| `--claude-model NAME`                             | Claude model alias (`sonnet` or `haiku`); resolves to a current model id (default `sonnet`) |
| `--gemini-model NAME`                             | Gemini model (default `gemini-2.5-flash`) |
| `--openrouter-model SLUG`                         | Any vision-capable OpenRouter slug (default `moonshotai/kimi-k3`) |

### Image handling

| Flag               | What it does |
|--------------------|---|
| `--max-edge N`     | Downscale the longest edge before upload; 0 keeps original size. Smaller is cheaper, larger reads fine print better (default 1024) |
| `--jpeg-quality N` | JPEG quality 1-100 for the uploaded copy (default 80) |

### Context

| Flag                        | What it does |
|-----------------------------|---|
| `--photo-context-text TEXT` | Inline background context, treated as authoritative |
| `--photo-context-file PATH` | Same, from a UTF-8 text file |

### Grouping and apply behavior

| Flag                                               | What it does |
|----------------------------------------------------|---|
| `--group-by {object,pair,none}`                    | Grouping granularity, the one axis (default `object`). `object`: every scan of one print is one object and shares a single analysis. `pair`: each rescan — print plus variant letter — is analyzed on its own. `none`: every file alone. See below |
| `--date-confidence-threshold X`                    | Minimum model confidence before a date guess is applied, 0-1 (default 0.7) |
| `--location-confidence-threshold X`                | Same, for location guesses (default 0.7) |
| `--no-update-vocab`                                | Don't append newly proposed keywords to the vocabulary file |

`--group-by` replaced `--process-all-variants` and `--update-policy`. Both are still accepted so nothing that passes them crashes, but they do nothing and each warns once. There is no replacement for "analyze one scan per group and copy the answer onto the rest": `object` sends the whole group, `pair` one call per rescan, `none` one call per file. `object` never costs more model calls than the old default did — it forms the same groups and makes one call each — but a group holding more than one scan of the print now sends every one of them, so a five-scan group costs five images on that one call instead of two.

### Output

| Flag                       | What it does |
|----------------------------|---|
| `--output-file PATH`       | `.ndjson` streams one record per finished photo; `.json` writes one aggregate object atomically. Works for every input type; without it, results go to stdout |
| `--output-sidecars`        | Also write a per-photo sidecar JSON next to each image (default off) |
| `--generate-manifest PATH` | Write the manifest folder or single-photo input would be grouped into, then exit without calling the model (not valid with manifest input) |
| `--batch-id ID`            | Identifier added to each record on the `.ndjson` streaming path, and used to name debug-dump files. It does not appear in the aggregate `.json` or on stdout |
| `--changeset {true,false}` | Emit a changeset NDJSON of proposed file writes, for every input type (default `false`) |
| `--dry-run`                | Print the plan summary and stop, before the first model call. Nothing is analyzed and no destination is touched. Beside `--generate-manifest`, reports the grouping it would write and leaves the file alone |

### ExifTool write-back

| Flag                            | What it does |
|---------------------------------|---|
| `-w`, `--write`                 | Shorthand for `--changeset true --exiftool-write true`: record the proposed writes and apply them. An explicit flag that contradicts it is an error rather than a guess |
| `--exiftool-write {true,false}` | Apply changeset fields to the files after analysis (default `false`; nothing is written without an explicit opt-in) |
| `--exiftool-fields TAGS`        | Comma-separated tags ExifTool may write (default `EXIF:UserComment`) |
| `--exiftool-path PATH`          | ExifTool binary to use (default: auto-detect) |

### Debug

| Flag                                    | What it does |
|-----------------------------------------|---|
| `--debug-dump-llm-request {true,false}` | Save full request payloads to disk before each model call (default `false`) |
| `--debug-dump-dir DIR`                  | Where those dumps go. Default depends on the input: `<dirname of --output-file, else of the manifest>/debug` for manifest input, and `./debug` under the working directory for folder and single-photo input |

## Providers

OpenAI (default), Anthropic, Gemini, and OpenRouter (any vision-capable slug — Kimi, Grok, Qwen, ...). Select with `--provider` or `LLM_PROVIDER`. Only the SDK for the provider you use needs to be installed, and only that provider's key needs to be set.

## Layout

| Layer | Where | What it does |
|---|---|---|
| Core library | `photokin/` | Prompts, provider dispatch, JSON parsing/repair, metadata merge, changeset emission. No ExifTool dependency. |
| ExifTool wrapper | `photokin/exiftool/` | Hydration (read before analysis) and apply (write after). |

Dependency direction: the wrapper imports from the core; the core never imports the wrapper. The CLI (`photokin/cli.py`) composes them into the full pipeline: hydrate, analyze, apply. Embedders who don't want ExifTool can call the core directly — `core.process_manifest_stream` takes any `metadata_hydrator` callable, or none.

## Tests

From the repository root:

```bash
python -m pytest
```

Runs `photokin/tests/` and `tests/`, which `pyproject.toml` sets as the test paths. Python 3.11+.
