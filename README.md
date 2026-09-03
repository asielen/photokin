# photokin

Run scanned photos and documents through a vision model and get archival metadata back: a verbatim transcription of whatever is written on the front or back, a scene caption, keywords, and deliberately cautious date/location guesses - as JSON, NDJSON streams, or metadata written straight into the files with ExifTool.

Compatible with OpenAI, Anthropic, Gemini, and OpenRouter API keys.

# Why should I use this?

I have inherited thousands of family photos and documents. I will never have time to review each one by hand, but an LLM first pass pulls the key data out of every scan and makes the eventual manual review far easier. This library automates that first pass.

## Quick start for a single photo

_You will need an LLM API key. This is separate from a chat subscription — API access is billed for what you use rather than a fixed monthly cost. Search "[provider] API key" and follow the provider's directions to set up billing and a key._

Install photokin and set up ExifTool:

```bash
pip install "photokin[openai]"          # or [anthropic] / [gemini] / [all]
export OPENAI_API_KEY=sk-...            # setx on Windows
python -m photokin.exiftool.fetch       # sets up ExifTool, on any OS
```

Two notes on the install lines:

- **ExifTool is part of a normal install.** It is how photokin reads the metadata your files already hold and how it writes results back into them — the whole archival workflow runs through it, so set it up unless you're embedding photokin in a tool that has its own metadata writer. The fetch command works on every OS: it downloads the official ExifTool release into `~/.photokin/bin`, verified against the SHA256 that exiftool.org publishes, with no system install needed — and on macOS/Linux it skips the download when an ExifTool is already installed (`brew install exiftool` counts).
  
- **photokin runs with the provider you installed.** With exactly one provider SDK installed, it is used automatically — no flag needed. With more than one (say, `[all]`), pick per run with `--provider anthropic`, or set it once with the `LLM_PROVIDER` environment variable and never type it again — see [Set your defaults once](#set-your-defaults-once), which also covers setting a default model. OpenRouter is the one exception: it shares OpenAI's SDK, so it always takes an explicit `--provider openrouter`.

Now run your first analysis:

```bash
photokin scan_042.jpg --back scan_042-back.jpg
```

This calls the model but does not modify your photos: the result is one JSON document printed to your terminal — keyed by image path, one entry per file, so the back gets its own record. The one file it may touch is photokin's own keyword vocabulary (part of its install, not your pictures), where a newly proposed keyword can be added — `--no-update-vocab` turns that off. (To also save what each model call was built from — the request payloads, the assembled metadata, and a log, in a `./debug` folder — add `-v`.) Abridged output:

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
    "scan_042-back.jpg": { "keywords": ["...", "back"], "...": "..." }
  },
  "errors": {}
}
```

The transcription (`caption`) and the interpretation (`ai_caption`) are kept strictly separate — the model is not allowed to "improve" what's actually written on the object. That separation is most of the reason this tool exists.

Happy with the result? Add `-rw` to the same command to also read the metadata the files already hold, analyze with that context, and write the results back into the files:

```bash
photokin scan_042.jpg --back scan_042-back.jpg -rw
```

**`-w` modifies your image files — back them up before your first writing run.** Exactly which fields land in which tags is listed in [What gets written where](#what-gets-written-where); folder runs are covered in [Folders and batches](#folders-and-batches).

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

Pick a folder to hold your virtual environment and any manifest/output files. This does *not* need to contain your actual photos — you'll point photokin at wherever those already live, by full path.

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

### 6. Set up ExifTool

ExifTool is what lets photokin read the metadata already in your files and write results back into them — the normal archival workflow needs it:

```powershell
python -m photokin.exiftool.fetch
```

This downloads the official ExifTool binary into `~/.photokin/bin` — no separate system install required.

Installing it does not turn writing on. Nothing is written to your files unless you add `-w` to a run, so the commands in step 7 still only read. See [Reading and writing your files](#reading-and-writing-your-files) for what `-w` writes and where.

### 7. Run it

Give it the full path to the photo — you're in `photokin-work`, not in your pictures folder, and the type is detected from the path you pass:

```powershell
photokin C:\Users\YourName\Pictures\scan_042.jpg --back C:\Users\YourName\Pictures\scan_042-back.jpg
```

or against a whole folder:

```powershell
photokin C:\Users\YourName\Pictures\Scans\ > results.json
```

No `--provider` flag needed: you installed exactly one provider SDK in step 4, so photokin uses it. If you later install a second one, pick per run with `--provider anthropic`, or set it once with `setx LLM_PROVIDER anthropic` (new terminals pick it up) — see [Set your defaults once](#set-your-defaults-once).

These runs only print results. When the output looks right, the normal archival run is the same command plus `-rw` — read what the files already hold, write the results back — covered in [Folders and batches](#folders-and-batches).

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

Pick a folder to hold your virtual environment and any manifest/output files. This does *not* need to contain your actual photos — you'll point photokin at wherever those already live, by full path.

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

### 6. Set up ExifTool

ExifTool is what lets photokin read the metadata already in your files and write results back into them — the normal archival workflow needs it:

```bash
python -m photokin.exiftool.fetch
```

The fetch command uses an ExifTool you already have when one is on your `PATH`; otherwise it downloads the official ExifTool distribution into `~/.photokin/bin`, verified against the SHA256 that exiftool.org publishes, and runs it on the `perl` every Mac ships with. If you prefer a system install, `brew install exiftool` works just as well — run the fetch command afterwards and it will simply confirm the one it found.

Installing it does not turn writing on. Nothing is written to your files unless you add `-w` to a run, so the commands in step 7 still only read. See [Reading and writing your files](#reading-and-writing-your-files) for what `-w` writes and where.

### 7. Run it

Give it the full path to the photo — you're in `photokin-work`, not in your pictures folder, and the type is detected from the path you pass:

```bash
photokin ~/Pictures/scan_042.jpg --back ~/Pictures/scan_042-back.jpg
```

or against a whole folder:

```bash
photokin ~/Pictures/Scans/ > results.json
```

No `--provider` flag needed: you installed exactly one provider SDK in step 4, so photokin uses it. If you later install a second one, pick per run with `--provider anthropic`, or set it once by adding `export LLM_PROVIDER=anthropic` to your `~/.zshrc` — see [Set your defaults once](#set-your-defaults-once).

These runs only print results. When the output looks right, the normal archival run is the same command plus `-rw` — read what the files already hold, write the results back — covered in [Folders and batches](#folders-and-batches).

### Coming back later

Each new session, just reactivate the environment before running photokin:

```bash
cd ~/photokin-work
source .venv/bin/activate
photokin ...
```

## Folders and batches

**The run you'll normally use is `-rw`:**

```
photokin C:\Users\YourName\Pictures\Scans\ -rw
photokin box3_017.jpg --back box3_017-back.jpg -rw
```

Point photokin at a folder and it works through every image in it (non-recursively). By default:

* All scans of one physical object — a front and its back, a rescan and the original — are sent to the model together and analyzed as one unit.
* The results are combined into one coherent set of analysis fields shared across the group — if the front and the back both carry writing, the caption of both files includes both transcriptions. Two things stay per-file: the `back`/`negative` part keyword, and, for documents, each page's own caption (see the exception below).
* Grouping goes by file name only, following the [naming conventions](#naming-conventions) below. The model is never asked to guess which files belong together.
* A group with no front scan — only pages, only a negative, only a back — is analyzed like any other object.

Multipage documents are the one exception:

* A group of page-numbered files (`letter-page1.jpg`, `letter-page2.jpg`, ...) is still analyzed together, but each page's caption gets only that page's own transcription — it doesn't make sense to write a 63-page story into the metadata of all 63 files. This is not configurable; the closest lever is `--group-by none`, which analyzes every file alone.
* To get a full, readable transcript, add `-s` (short for `--sidecar-md auto`), which writes a Markdown transcript file beside each page when the model categorizes the object as a `Document` or `Postcard` — see [A readable transcript beside each scan](#a-readable-transcript-beside-each-scan). It combines with the other short flags, so the archival run with transcripts is `-rws`.

**A run without `-w` still calls the model but writes nothing back.** `photokin C:\Scans` analyzes, prints the JSON to your terminal and touches nothing. Its plan summary ends with one extra row naming the next step:

```
[INFO] Plan for this run:
  input     : C:\Scans (folder, 12 file(s) in 5 group(s), group-by object)
  read      : none (-r not given)
  output    : stdout
  changeset : none (--changeset false)
  write     : none
  provider  : ChatGPT
  model     : gpt-4o
  note      : this run only prints results - your photos are not read or
              changed. For the normal archival run:
                  photokin "C:\Scans" -rw
```

The printed result has one entry per file — backs, variants, pages, negatives and crops included — with diagnostics and summaries going to stderr. To write results to a file instead of the terminal, see [--output-file](#advanced-usage).

## Naming conventions

Grouping is driven entirely by filename suffixes. The grammar is `name[letter][-front|-back|-negative|-pageN][-crop]`, case-insensitive, applied right to left:

| Example | Meaning |
|---|---|
| `box3_025.jpg` | the photo itself (the print's front, no variant letter) |
| `box3_025-b.jpg` or `box3_025b.jpg` | another scan of the same object (variant letter, with or without dash after a digit) |
| `box3_025-back.jpg` | the reverse side (`-front` and `-negative` work the same way) |
| `album-page1.jpg`, `album-page2.jpg` | ordered pages of one document |
| `box3_025-back-crop.jpg` | a cropped detail of its parent, recorded with the group but not analyzed as its own object while its parent is present — a crop with no parent fills the missing slot, and `--group-by none` analyzes every crop alone |
| `box3_025.tif` beside `box3_025.jpg` | the same scan in two formats — one object, not two photos |

The variant letter comes before the part suffix (`025b-back-crop.jpg`), and a file with no explicit `-pageN` is only treated as page 1 if its group contains other numbered pages.

Every file also gets at most one keyword naming its part — `back` on a reverse side, `negative` on a negative, nothing on a front — so you can always tell which file is which afterwards. A `back` or `Negative` keyword you applied yourself is left exactly where it is.

**Same name, different extension — one object, one call.** A TIFF master beside the JPEG made from it is one scan of one print, so the pair claims one place in the group. The higher-fidelity file is sent to the model (TIFF first, then PNG, then the lossy formats) and the analysis is written to both — a folder of 200 TIFF/JPEG pairs uploads 200 images instead of 400. The run names each file it didn't upload and counts them in the closing `N file(s) recorded without being sent to the model` line; that number is the saving, not a warning. Images are also downscaled before upload to save tokens and bandwidth — tune that with `--max-edge` and `--jpeg-quality` (see [Image handling](#image-handling)).

## Choosing how to group objects

`--group-by` is the one grouping axis:

* `object` (default) — every scan of one object is one group, and the whole group goes to the model in one call, so it can read detail off whichever scan came out clearest.
* `pair` — keeps a front with its back, but analyzes each rescan on its own.
* `none` — no grouping at all: every file is analyzed alone, and every crop becomes its own object.

Files that don't follow the naming conventions effectively run as `--group-by none`.

## Reading and writing your files

ExifTool reads the metadata a file already holds (`-r`) and writes the analysis back into it (`-w`); the two combine as `-rw`.

### What gets written where

| Result field | Tag |
|---|---|
| `ai_caption` (the AI analysis) | `EXIF:UserComment` |
| `caption` (the verbatim transcription) | `XMP-dc:Description` |
| `keywords` | `XMP-dc:Subject` |
| `title` | `XMP-dc:Title` |
| `date_guess` (when confident enough) | `EXIF:DateTimeOriginal` |
| `location_guess` (when confident enough) | `IPTC:Country-PrimaryLocationName` / `Province-State` / `City` / `Sub-location` — not written by default: `-r` doesn't read these tags yet, so a location already in the file would be overwritten unread; opt in by naming them in `--exiftool-fields` — which replaces the whole list, so include the default tags you still want alongside them |

### Why read first

With `-r`, photokin reads `EXIF:DateTimeOriginal`, `EXIF:UserComment`, `XMP:Description`, `XMP:Title` and `XMP:Subject` before analysis, so a note, caption, title, date or keyword already living in an image rides along to the model as context. Two of those fields get special treatment:

* **Dates.** The file's own date is treated as evidence, not truth: on a flatbed scan `DateTimeOriginal` is the day you scanned the print, not the day the photograph was taken, so it never overwrites the model's `date_guess` — but it is what the date-correction heuristic compares that guess against before writing anything.
* **Titles.** Scanner software routinely writes "Scanned Image" or the bare filename into `XMP:Title`, so a title read out of a file does not beat the model. A title you supplied yourself, in a manifest or `--meta`, always wins.

A file `-r` asked for but could not read gets **no proposed writes at all**, with a warning naming it — unread is not empty, and writing against a before-snapshot that was never seen could overwrite metadata the file really holds. Fix whatever blocked the read (a locked or corrupt file) and re-run.

### If a write fails

Files are written independently — a failure on one never touches its neighbors. To find out what happened and pick up again:

* The per-file reasons are logged as `[ExifTool] Errors:` before the run ends. Treat a listed file as unverified rather than untouched: ExifTool can write some of a file's tags and miss others in the same pass, so photokin counts nothing on that file as written — re-running it is the fix, not proof that something was lost.
* If writes were attempted and **none** succeeded, the run exits 2: some setting is wrong for every file (an unwritable `--exiftool-fields` tag, a read-only folder, a binary that will not run). Fix the setting and re-run. A run whose changeset simply proposed nothing to write exits 0.
* If only **some** files failed, the run exits 0: the settings were right, and one locked or corrupt file among many is ordinary. Fix those files and re-run the same command — captions merge instead of duplicating (see [Captions](#captions)), so already-written files come out unchanged. Note a re-run does call the model again for every group.

Manifest mode is the exception and always exits 0: the Lightroom plug-in reads per-item records, not exit codes.

## Captions

For a group of scans of one object, photokin merges what you already wrote with what the model transcribed, and writes the same caption block to every file in the group.

### The shape

Take a print scanned twice, plus the back of the second scan — `box3_017.jpg`, `box3_017b.jpg`, `box3_017b-back.jpg`. After a run, every one of the three files holds the same block:

```
[Photo A] Caption A
[Photo B] Caption B
[Back] Back of Photo B
```

Those files are one physical photograph, and which one you open a year from now is an accident of browsing — any of them should tell the object's whole story. The block is pure transcription: your captions plus the model's reading of what is written on the object, written to `XMP-dc:Description`. The model's interpretation of the scene (`ai_caption`) goes separately to `EXIF:UserComment`, never here.

Labels are only added when there is something to tell apart. A lone scan with no back — the overwhelmingly common case — keeps its caption exactly as you typed it, no brackets. One photo plus its back gets `[Photo]` / `[Back]`; lettered variants get `[Photo A]` / `[Photo B]`, matching the letters on disk (a bare scan is variant A).

**A caption you typed on one file will appear on its siblings.** They are one object, so the front's "Ruth and Sam outside the bakery" ends up on the back scan too. If you don't want two files sharing a caption, they aren't one object as far as photokin is concerned — split the group, or run with `--group-by none`.

### Your existing captions are kept

Nothing you wrote is ever deleted. A new transcription that differs from your caption is added beside it under its own label; one that matches it (ignoring punctuation, spacing, quoting and capitalization) is dropped as a duplicate. Anything that changes a word is kept — `bakery, 1948` against `bakery, 1949` is a different caption. If you reword a caption and want the old one gone, delete it yourself; photokin will not guess that a rewrite meant replace.

Running `-rw` repeatedly does not grow your captions: labelled lines are recognized as photokin's own and merged section by section, so after the first write the block is stable byte for byte. (A stray `[AI Analysis]` tail written into Description by an older release is recognized and stripped on the next read.)

### Documents get their own page, not the whole book

Each page of a multipage document carries only its own transcription in `XMP-dc:Description` — you opened page 37 to read page 37, not the whole 63-page letter. Anything that is not an ordered sequence of pages (a front/back pair, a rescan, a variant) still gets the shared block above. An archive processed by an older photokin keeps the whole-document captions it already holds; re-running does not clear them.

## A readable transcript beside each scan

The caption block lives inside `XMP-dc:Description` — readable with a metadata viewer, not by opening a file. `--sidecar-md` writes the same transcription as its own Markdown file beside each analyzed image, with frontmatter carrying that file's metadata (title, category, keywords, date, location, group, page number, and which model produced it):

- `off` (the default) — nothing new is written.
- `auto` — only for a group whose category comes back `Document` or `Postcard`. `-s` is shorthand for this one, and combines with the other short flags: `photokin ./scans -rws` is the archival run with transcripts.
- `all` — a sidecar for every emitted file, any category, except crops.

Whether a sidecar is written under `auto` is decided purely by the model's category verdict for the group — the page-numbered filenames play no part in it (they decide what each page's sidecar and caption *contain*). An explicit `--sidecar-md off` or `all` beside `-s` is refused as a contradiction, the same way `-w` and `-v` treat theirs.

```bash
photokin letter.jpg --sidecar-md all
```

That writes `letter.md` beside `letter.jpg`. The frontmatter's exact shape and chunked-document details are covered under [Markdown transcript sidecars](#markdown-transcript-sidecars-for-documents) in Advanced usage.

## Managing API keys

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

You only need the key for the provider you're actually using. It's worth setting a spend limit in your provider's dashboard. 


## Advanced usage

Everything below is opt-in/opt-out machinery for auditing, redirecting output, and bigger or more repeatable jobs. None of it is needed for the normal `-rw` run.

### Previewing a run: --dry-run

`--dry-run` prints the plan summary — input, grouping, read set, write set, and the exact output and changeset paths the run would use — and stops before the first model call. Nothing is analyzed, nothing is written, nothing is spent. It is the way to check where a batch's writes and changesets would land before committing to it. Beside `--generate-manifest`, it reports the grouping that would be written and leaves the file alone.

### Redirecting output: --output-file and sidecars

`--output-file` works for every input type — a folder, a manifest or one photo: a `.ndjson` path streams one record per finished photo (you can watch progress, and a crash doesn't lose completed work), while a `.json` path writes a single aggregate object atomically at the end. With it, stdout stays empty.

The changeset follows the output file: a changeset is otherwise written beside the *input*, so `--changeset true` on a photo folder drops the `.ndjson` inside that folder — pass `--output-file` a path in a folder you control and the changeset lands there instead.

`--output-sidecars` additionally writes a per-photo sidecar JSON next to each image (default off).

Every destination the run writes — `--output-file`, `--generate-manifest`, `--log-file`, the changeset — is checked against the run's inputs and its other destinations before being opened; a collision is refused (exit 2, naming both paths) rather than overwriting a file the run depends on. `--dry-run` runs the same check.

### Changesets: an audit trail for writes

`--changeset true` emits a changeset NDJSON alongside the results: a record of proposed field writes that the ExifTool wrapper can apply to the files, either in the same run (`-w`, or `--exiftool-write true --exiftool-fields EXIF:UserComment`) or later and separately. It is written to `dirname(--output-file or input)` as `<stem>_changeset.ndjson` — so `--output-file results.ndjson` yields `results_changeset.ndjson`, and `photokin ./scans/ --changeset true` yields `scans_changeset.ndjson` inside the folder.

Since the changeset is a plain record of proposed writes, you can inspect it first and apply it separately:

```bash
python -m photokin.exiftool --changeset results_changeset.ndjson --enabled --dry-run   # counts what would be written
python -m photokin.exiftool --changeset results_changeset.ndjson --enabled            # actually writes
```

The standalone applier also takes `--fields` to narrow which tags may be written, `--write-sidecar-only` to write `.xmp` sidecars instead of touching the originals, `--no-overwrite-original` to keep ExifTool's `_original` backup files, and `--output summary.json` for a machine-readable result. Date tags (`EXIF:DateTimeOriginal`, `EXIF:CreateDate`) are normalized to EXIF's `YYYY:MM:DD HH:MM:SS` format on the way in; unparseable dates become warnings, not writes.

### Manifest mode

A manifest is a JSON file listing exactly what to process — an `items` array where each entry needs only a `path`. It is mainly how another program drives photokin (the Lightroom plug-in works this way; see [Integrating photokin as a subprocess](#integrating-photokin-as-a-subprocess)), and it also makes a big job repeatable and editable. `--generate-manifest` turns a folder into exactly that file:

```bash
photokin ./scans/ --generate-manifest scans-manifest.json
```

It writes the manifest the folder run would have used — same files, same order — and exits without calling the model, so it costs nothing and doubles as a way to check the grouping before committing to a batch. Edit it (add `is_back`, `group`, existing `metadata`) and feed it straight back: `photokin scans-manifest.json`.

The sample below declares one physical object, a front scan and its back, and one line of batch-wide background context. Note the underscore in `box3_017_back.jpg`: the filename grammar reads only the hyphenated `-back`, so it is the `is_back` flag that folds the two files into one group and one model call rather than two unrelated photos.

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
### Photo flags
Flags are optional when the filename already says the same thing; they exist so files that don't follow the naming conventions can still be grouped correctly. An explicit flag always beats the filename, in both directions and including when the two contradict each other — anything else would leave the flag inert in exactly the situation it is there for. Every override that changes what the filename implied is logged, so a typo is visible rather than silent.

| Key | Effect |
|---|---|
| `is_back` | `true` marks the reverse side, `false` marks the front. `true` also repairs the group key by stripping a trailing `back` token, which is what puts `box3_017_back.jpg` in the same group as `box3_017.jpg`. |
| `is_crop` | `true` marks a cropped derivative, so the file is recorded with its group but not analyzed; `false` unmarks a file whose name ends in `-crop`. |
| `version` | The variant id, replacing any letter read off the filename. Any string, not just one letter; empty means no variant. |
| `group` | The group key outright, for names the grammar cannot parse at all. `base_id` is accepted as an alias and loses to `group` when both are given. |
| `preferred` | Breaks a tie between two files claiming the same slot — the same side of the same variant — so the one you name is the one sent and the other is recorded and warned about. It chooses between candidates; it cannot create a place for one. See below. |

`is_back` and `is_crop` may be written as JSON `true`/`false`, as `0`/`1`, or as the strings `"true"`, `"false"`, `"yes"`, `"no"`. A `null` value means "not specified" and leaves the filename in charge.

`preferred` is the exception and does not read that grammar: it is plain truthiness, so any non-empty string sets it and `"preferred": "false"` means **true**. Write it as a JSON `true`, or leave the key out entirely. It also nominates the file the group's analysis is filed under.

`preferred` chooses between candidates for a slot; it cannot create one. A crop always yields to its listed parent, and a file with no part left to claim (a plain `album.jpg` beside an explicit `album-page1.jpg`) cannot be promoted into one. Both cases log a warning naming the file and are listed in the result record under `all_variant_files.crops` / `all_variant_files.displaced`, so nothing disappears quietly.

**Replaying a manifest.** A manifest run with `-r` also records what ExifTool read into the output document, so replaying that manifest later (`photokin scans-manifest.json`) needs no ExifTool at all. Replay *with* `-r` to reproduce the original result exactly.

### Markdown transcript sidecars for documents

`--sidecar-md {off,auto,all}` (default `off`) writes `<stem>.md` beside each analyzed image — the same path derivation `--output-sidecars` uses for `<stem>.json`, and the same failure contract: an unwritable destination logs a warning and does not take the analysis down with it.

`auto` gates on the group's own `category` result — only `Document` and `Postcard` trigger it, the two categories that are mostly text; `Photo Page` (an album page with mounted photos and typed captions) deliberately does not. `all` ignores category and writes for every emitted file.

Crops never get a sidecar — a crop isn't analyzed on its own, so its sidecar would only duplicate its parent's.

Frontmatter carries the same values the changeset would write for that file, plus the structural facts that place it in its group: group id, part label, page number, page count, and every filename in the group. A worked example, page 2 of a six-page letter:

```yaml
---
source_file: "box3_017-page2.jpg"
group: "box3_017"
part: "Page 2"
page: 2
page_count: 6
group_files: ["box3_017-page1.jpg", "box3_017-page2.jpg", "box3_017-page3.jpg", "box3_017-page4.jpg", "box3_017-page5.jpg", "box3_017-page6.jpg"]
title: "Letter from Ruth, November 1944"
category: "Document"
keywords: ["Document", "Ruth", "Le Mans", "1944"]
date: "1944-11-27"
date_pattern: "Y!M!D!"
date_confidence: 0.95
location: {country: "France", city: "Le Mans", confidence: 0.9}
analyzed_by: "Claude claude-sonnet-4-6 (2026-08-27)"
---

# Letter from Ruth, November 1944

[AI Analysis]: A handwritten letter, three pages, in a woman's hand...

## Transcription — Page 2

Dear Mother,

We arrived in Le Mans yesterday, tired but glad to be off the train at last.
```

A key with nothing to say is omitted — a file with no location guess writes no `location` key. When a chunked document's consolidation pass (see below) corrects a page number, `page` carries the corrected value and the filename's own number is kept alongside as `page_from_filename`. When nothing can be attributed to this file specifically, the body falls back to the whole group's caption block and the frontmatter marks it `transcription_scope: group`.

A sidecar is derived output, the same as the JSON one `--output-sidecars` writes: a re-run overwrites it outright rather than merging with what's already there, unlike the caption block written into the image itself, which is merged section by section (see [Captions](#captions)).

### Large documents: `--max-images-per-call`

One model call ordinarily carries a whole group, however large — a 63-page memoir would be one call holding 63 images. `--max-images-per-call N` (default `8`) splits an oversized group into several calls. It never splits mid-page, a page's own rescans never straddle a block, and a front, back and negative always ride together in the first call. After the last chunk, one further **text-only** call consolidates the chunks' provisional keywords/title/category/date/location into the group's one final answer. It does not re-transcribe anything — the per-chunk transcriptions stand as written.

A group at or under the cap is entirely unaffected, and `--max-images-per-call 0` disables chunking outright. Chunking sends the same number of images either way; it adds the repeated prompt on every chunk call plus the consolidation call's tokens, and buys per-page attention that doesn't thin out on long documents, payloads under provider size ceilings, and failures that name which chunk failed.

The consolidation pass's page-order verdict is **recorded, never acted on**: when the pages read out of filename order, the corrected page number goes into the record and the sidecar's `page` field with a warning naming the group, but no file is renamed or renumbered — that stays a decision for a person.

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
| `--provider {openai,anthropic,gemini,openrouter}` | Which backend to call. Default: `LLM_PROVIDER` if set, else the one provider whose SDK is installed; with several installed the choice is required. See [Providers](#providers) |
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

If you are processing a large number of photos related to a single event, you can add context around that event that will be shipped with the LLM call. For example if the whole photo set is part of a wedding. The context could include dates, locations, people to help make the LLMs job easier.

| Flag                        | What it does |
|-----------------------------|---|
| `--photo-context-text TEXT` | Inline background context, treated as authoritative |
| `--photo-context-file PATH` | Same, from a UTF-8 text file |

### Grouping and apply behavior

| Flag                                               | What it does |
|----------------------------------------------------|---|
| `--group-by {object,pair,none}`                    | Grouping granularity, the one axis (default `object`). `object`: every scan of one print is one object and shares a single analysis. `pair`: each rescan — print plus variant letter — is analyzed on its own. `none`: every file alone. See below |
| `--date-confidence-threshold X`                    | Minimum model confidence before a date guess is written into a file that has no date, 0-1 (default 0.6). Replacing a date the file already holds is governed separately and costs more; see below |
| `--location-confidence-threshold X`                | Same, for location guesses (default 0.7) |
| `--no-update-vocab`                                | Don't append newly proposed keywords to the vocabulary file |

### Output

| Flag                       | What it does |
|----------------------------|---|
| `--output-file PATH`       | `.ndjson` streams one record per finished photo; `.json` writes one aggregate object atomically. Works for every input type; without it, results go to stdout |
| `--pretty-json {true,false}` | Indent the stdout result document (and an aggregate `.json` `--output-file`) for human reading (default `true`). Pass `false` for compact single-line output, e.g. when a script parses stdout itself rather than reading it with a JSON library |
| `--output-sidecars`        | Also write a per-photo sidecar JSON next to each image (default off) |
| `--sidecar-md {off,auto,all}` | Also write a per-part Markdown transcript sidecar next to each image. `off`: nothing (default). `all`: every emitted file except crops. `auto`: only for a group whose category is `Document` or `Postcard` |
| `-s`                       | Shorthand for `--sidecar-md auto`; combines with the other short flags as `-rws`. A `--sidecar-md` value that contradicts it is an error rather than a guess, and like `-w` and `-v` it is refused beside `--generate-manifest`, which makes no model call |
| `--max-images-per-call N`  | Cap on images sent in one model call. A group whose payload exceeds it is split into contiguous chunks (a front/back pair is never split across chunks) plus one text-only consolidation call that merges the chunks' metadata and corrects page order; a group at or under it is unaffected (default 8, `0` disables chunking) |
| `--generate-manifest PATH` | Write the manifest folder or single-photo input would be grouped into, then exit without calling the model (not valid with manifest input) |
| `--batch-id ID`            | Identifier added to each record on the `.ndjson` streaming path, and used to name debug-dump files. It does not appear in the aggregate `.json` or on stdout |
| `--changeset {true,false}` | Emit a changeset NDJSON of proposed file writes, for every input type (default `false`) |
| `--dry-run`                | Print the plan summary and stop, before the first model call. Nothing is analyzed and no destination is touched. Beside `--generate-manifest`, reports the grouping it would write and leaves the file alone |

`sidecar-xmp` and `sidecar-json` are **reserved** spellings in this same
family (not yet flags photokin accepts) — XMP for standard metadata sidecars
when they arrive, JSON for the day `--output-sidecars` is folded in as an
alias of `sidecar-json all` — the way `-R` is reserved below, and must not be
spent on anything else.

### ExifTool read and write-back

| Flag                            | What it does |
|---------------------------------|---|
| `-r`, `--read`                  | Before analysis, read `EXIF:DateTimeOriginal`, `EXIF:UserComment`, `XMP:Description`, `XMP:Title` and `XMP:Subject` out of the files and send them to the model, for every input type. Only fills what the input does not already carry; nothing is written. Mirrors `-w` |
| `-w`, `--write`                 | Shorthand for `--changeset true --exiftool-write true`: record the proposed writes and apply them. An explicit flag that contradicts it is an error rather than a guess |
| `--exiftool-write {true,false}` | Apply changeset fields to the files after analysis (default `false`; nothing is written without an explicit opt-in) |
| `--exiftool-fields TAGS`        | Comma-separated tags ExifTool may write. The default is every tag in the [What gets written where](#what-gets-written-where) table except the location tags — those are an explicit opt-in until `-r` learns to read them, so a curated location is never overwritten unread. The flag **replaces** the default list rather than adding to it, so name every tag you want written. A launcher that writes some tags itself (the Lightroom plug-in writes the XMP tags through the catalog SDK) should narrow this explicitly, e.g. `--exiftool-fields EXIF:UserComment` |
| `--exiftool-path PATH`          | ExifTool binary to use (default: auto-detect) |

`-r` is the read half and `-w` the write half; the short letters are deliberately symmetrical, and they combine as `-rw` (or `-wr`) exactly like any other pair of short flags — that combined form is the one to reach for, for the reason given above. `-R` is **reserved** for the recursive-folder flag that is still deferred (it changes grouping semantics across directories and interacts with write safety, so it gets its own change), and must not be spent on anything else.

### Rename mode [BETA FEATURE]

See [Rename mode: `--rename`](#beta-feature-rename-mode---rename) below for what it does. `--rename` is a mode flag: like `--generate-manifest`, it stops the run before any model call, and it takes a folder or manifest input — not a single photo.

| Flag                    | What it does |
|--------------------------|---|
| `--rename PREFIX`       | Plan a grammar-aware mass rename of the input folder or manifest's files under `PREFIX`; print the preview and stop. `-w` applies it. `--exiftool-write` and `--output-file` are refused beside it — rename mode writes no tags, and `--plan-out` is its own destination |
| `--digits N`            | Zero-padded number width (default 3) |
| `--order {name,natural}` | Fallback ordering when no item carries an explicit manifest `order` (default `name`). `natural` compares digit runs numerically, so `file9` precedes `file10` |
| `--undated LITERAL`     | Stand in for `{date}` in a group with no date, instead of refusing to plan it; those groups form their own numbering bucket |
| `--today YYYY-MM-DD`    | Override `{today}` (default: the run's own date), so a batch scanned earlier can carry its own date and a plan stays reproducible |
| `--companions EXT[,EXT]` | Extra non-image extensions carried along with a renamed image, beyond the default `.md`, `.json`, `.xmp`, `.txt` |
| `--plan-out PATH`       | Write the plan as JSON to `PATH` (see [`docs/rename-contract.md`](https://github.com/asielen/photokin/blob/v0.6.0/docs/rename-contract.md)), instead of — or beside — the preview table |
| `--rename-undo [JOURNAL]` | Reverse the latest applied rename in the positional folder, or the named journal file |
| `--rename-resume [JOURNAL]` | Finish an interrupted rename run in the positional folder, or the named journal file, forward |
| `--rename-finish PLAN`  | Rename only the companions of a `--rename` plan whose images a catalog application has already renamed |

### Debug

| Flag                                    | What it does |
|-----------------------------------------|---|
| `--debug-dump-llm-request {true,false}` | Save full request payloads to disk before each model call (default `false`) |
| `--debug-dump-dir DIR`                  | Where those dumps go. Default depends on the input: `<dirname of --output-file, else of the manifest>/debug` for manifest input, and `./debug` under the working directory for folder and single-photo input |

## Providers

OpenAI, Anthropic, Gemini, and OpenRouter (any vision-capable slug — Kimi, Grok, Qwen, ...). Only the SDK for the provider you use needs to be installed, and only that provider's key needs to be set.

**Which provider a run uses** is decided in this order: the `--provider` flag, else the `LLM_PROVIDER` environment variable, else the provider whose SDK is installed. With exactly one SDK installed there is nothing to say — installing `photokin[anthropic]` was already the choice. With several installed (or none) and nothing chosen, the run stops with exit 2 before spending anything, and the error says how to choose. OpenRouter is the one provider never picked automatically: it speaks the OpenAI-compatible API through the `openai` SDK, so install the `[openai]` extra, set `OPENROUTER_API_KEY`, and select it explicitly.

### Set your defaults once

The provider and each provider's model have an environment variable behind the flag, so a machine that always uses the same setup never types either:

| Setting | Flag (per run) | Env var (set once) | Default |
|---|---|---|---|
| Provider | `--provider` | `LLM_PROVIDER` | the one installed SDK |
| OpenAI model | `--openai-model` | `OPENAI_MODEL` | `gpt-4o` |
| Claude model | `--claude-model` | `CLAUDE_MODEL` | `sonnet` |
| Gemini model | `--gemini-model` | `GEMINI_MODEL` | `gemini-2.5-flash` |
| OpenRouter model | `--openrouter-model` | `OPENROUTER_MODEL` | `moonshotai/kimi-k3` |

Flags beat env vars, which beat the defaults. The whole set-and-forget setup is the API key plus these two variables. On Windows (new terminals pick them up):

```powershell
setx ANTHROPIC_API_KEY "sk-ant-..."
setx LLM_PROVIDER anthropic
setx CLAUDE_MODEL haiku          # optional - sonnet is the default
```

On macOS/Linux, the same three as `export` lines in `~/.zshrc` or `~/.bashrc`:

```bash
export ANTHROPIC_API_KEY="sk-ant-..."
export LLM_PROVIDER=anthropic
export CLAUDE_MODEL=haiku        # optional - sonnet is the default
```

After that, `photokin ./scans/ -rw` is the entire command, every time. (`CLAUDE_MODEL` also accepts a full `claude-*` model id, not just the `sonnet`/`haiku` aliases — useful for a model newer than photokin's pins.)

Providers retire and rename models over time — OpenRouter slugs especially come and go. When that happens to the model a run asked for (or to photokin's own pinned default), the run stops on the **first** model call with a `model_not_found` error naming the flag and env var to pick a current one, rather than failing every photo in the batch the same way.

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

## Integrating photokin as a subprocess

This section is for a plugin or script that launches `photokin` (or `python -m photokin.cli`) as a subprocess, cannot read a return value, and often cannot even read stderr — the Lightroom plugin this was built for launches fire-and-forget (`start /B` on Windows, output discarded) and learns what happened only from the files photokin wrote. Everything here exists to make that mode of use safe and observable.

### The problem this solves

Without what follows, a launcher watching only `--output-file` has one signal: did the file appear, and does it have as many lines as the manifest has items. That answers "it worked" but not "it is still running" versus "it already failed" versus "it will never appear" — an unknown flag, a bad manifest, a missing provider key, and a dead ExifTool binary all look identical: no file, forever. The pieces below close that gap.

### The run envelope

Whenever `--output-file` names a `.ndjson` destination, the file carries `run: ...` records interleaved with the normal per-file `path`/`status` records — the same file, not a second one, so a caller tailing it sees everything in one stream:

| `run` value | When | Carries |
|---|---|---|
| `start` | As early as the destination is known — before almost every pre-flight check, including ones that used to leave no trace at all (an unknown flag, an unwritable ExifTool tag, a missing or ambiguous provider, a missing ExifTool binary, a malformed manifest) | Nothing beyond the envelope fields below |
| `plan` | Once every pre-flight check has passed, right after the plan summary is logged | `plan`: the same fields as the stderr plan summary, as a dict (`input_kind`, `file_count`, `provider`, `model`, ... — see `RunPlan` in `cli_messages.py`) |
| `progress` | Once per group, right before it starts | `group`, `index`, `of` — a liveness signal for a group whose single model call may run for minutes with nothing else on the stream to show it hasn't died |
| `exiftool_apply` | After `-w` applies the changeset, if one was written | `summary`: files seen/written, tags written, errors, warnings |
| `complete` | The run finished (whether or not every group succeeded — "every group failed" is not a fatal error in manifest mode; see [When calls fail](https://github.com/asielen/photokin/blob/v0.6.0/photokin/README.md#when-calls-fail)) | `files_recorded`, `groups_failed`, `files_unsent` |
| `cancelled` | The run stopped early via `--cancel-file` (below) | Same three fields, counting only what completed before the stop |
| `fatal` | Any refusal or unrecoverable error, at any point after `start` | `error`: `{"type": ..., "message": ...}` |

A run always ends with exactly one of `complete`, `cancelled`, or `fatal` — a caller can wait for any of the three as the definitive "done" signal, rather than inferring completion from the line count, which breaks the moment per-file emission ever changes shape (this happened once already, before the envelope existed).

Two destinations are deliberately exempt. `--dry-run` never opens the envelope — that flag's whole point is that nothing is touched, and the envelope is a destination like any other. `--generate-manifest` beside `--output-file` is refused outright before either can be written (see [All flags](#output)), so there is never a results file for it to open.

One safety property carries over unchanged: a **pre-existing** `--output-file` is left completely untouched by a refusal. The envelope opens immediately only for a destination that does not exist yet; for one that does, it opens only once every check has passed and the run is committing to overwrite it anyway — at that point it gets the same `start`/`plan` records a fresh destination got immediately.

Every record — envelope and per-file alike — carries `schema_version` (currently `3`) and, when `--batch-id` was given, `batch_id`. `schema_version` bumps whenever a record's shape changes in a way a consumer could care about; see `--capabilities` below for a caller that wants to check compatibility rather than discover it the hard way, the way `photokin/README.md`'s `## Providers` section describes an older mismatch doing.

Per-file error payloads also carry two optional fields beyond the `type`/`message` documented under [When calls fail](https://github.com/asielen/photokin/blob/v0.6.0/photokin/README.md#when-calls-fail): `provider_message` (the provider's own error text, extracted from the SDK's structured response rather than read off a Python exception's `str()`, which for these SDKs is often the whole body rendered as a dict repr) and `retry_after` (seconds, when the provider's response included one — reliably available for OpenAI and Anthropic, not for Gemini).

### Cancelling a run in progress: `--cancel-file PATH`

Photokin polls for this path once before each group starts (never mid-group — a group is one model call under the default `object` grouping, so there is no narrower point to check). Once the file exists, the run stops cleanly: whatever completed is kept, `-w`'s ExifTool apply still runs over it, the envelope closes with `run: cancelled` instead of `run: complete`, and the process exits 0. Nothing is spent on groups that hadn't started yet.

```bash
photokin batch.json -rw --output-file results.ndjson --cancel-file results.ndjson.CANCEL
# from another process, at any point:
touch results.ndjson.CANCEL   # or: New-Item on Windows
```

### Debugging a run: `-v` and its parts

`-v` / `--verbose` bundles three things — the same relationship `-w` has to `--changeset`/`--exiftool-write` — so a caller that wants everything a run could leave behind for debugging asks for it with one flag instead of three:

| Flag | On its own | Under `-v` |
|---|---|---|
| `--debug-dump-llm-request {true,false}` | Write the full provider request payload (assembled prompt, images) to disk before each model call | `true` |
| `--debug-dump-hydration {true,false}` | Write each group's assembled metadata to disk before it is merged into a prompt — what `-r` read plus whatever the manifest supplied, one step upstream of the request dump | `true` |
| `--log-file PATH` | Duplicate the run's log output into this file, in addition to stderr | Defaults to `<debug-dump-dir>/<batch-id or "run">.log` if not given explicitly |

All three dumps land in `--debug-dump-dir` (default `./debug`, or `<manifest/output-dir>/debug` for manifest input), one folder per run holding everything: the LLM requests, the pre-prompt metadata, and now the log. An explicit value for any of the three individual flags always wins over what `-v` would otherwise set, and an explicit value that contradicts `-v` (`-v --debug-dump-hydration false`) is refused rather than silently picked between, the same as `-w` beside an explicit `--changeset false`. Like the write bundle, none of the three do anything useful without a model call, so all three are refused beside `--generate-manifest` — except a truly *explicit* `--log-file`, which still attaches, since even a `--generate-manifest` run has something worth logging.

### Checking compatibility: `--capabilities`

```bash
photokin --capabilities
```

Prints this build's contract as JSON and exits, before any input is required — the same way asking for help does:

```json
{
  "version": "0.6.1",
  "ndjson_schema_version": 3,
  "changeset_schema_version": 2,
  "canonical_tags": {
    "ai_caption": "EXIF:UserComment",
    "caption": "XMP-dc:Description",
    "keywords": "XMP-dc:Subject",
    "title": "XMP-dc:Title",
    "date_guess": "EXIF:DateTimeOriginal",
    "location_guess": {"country": "IPTC:Country-PrimaryLocationName", "state": "IPTC:Province-State", "city": "IPTC:City", "sublocation": "IPTC:Sub-location"}
  },
  "providers": ["openai", "anthropic", "gemini", "openrouter"],
  "flags": ["--back", "--batch-id", "..."]
}
```

Meant to replace an install-time probe (importing some internal symbol and trusting a pip version pin to mean everything else still matches) with a real, versioned answer a launcher can gate a run on instead of discovering a mismatch mid-batch. `canonical_tags` in particular is worth checking before a batch: an earlier photokin release wrote the wrong ExifTool tag spelling entirely (`XMP:dc:Description` instead of the writable `XMP-dc:Description`), which silently dropped every caption it tried to write rather than failing — a `--capabilities` check catches that class of mismatch instead of losing data quietly. `flags` is read live off the argument parser, so it can never drift from what the installed build actually accepts.

### A clean refusal for a headless launcher: empty or malformed argv

Running `photokin` with no arguments at all normally prompts on stdin — a courtesy for a human at a keyboard. A subprocess launcher has no keyboard, so `photokin` checks `sys.stdin.isatty()` first: with no terminal attached, an empty argument list is a usage error (exit 2) instead of a stdin read that would just hang. Separately, an argument list argparse cannot parse at all (an unknown flag, most often the result of a quoting bug upstream) still exits 2 with nothing on `--output-file` to read — argparse rejects the whole invocation before this module ever learns what the destination was meant to be — but a best-effort scan for `--output-file` in the raw arguments means even this earliest failure usually still lands a `run: start` + `run: fatal` pair in the results file, rather than leaving no trace anywhere a launcher can see.


### BETA FEATURE: Rename mode: `--rename`

`--rename PREFIX` cleans up and renumbers a folder's files under a prefix you choose, keeping every variant tag the naming grammar above already understands and closing the gaps in the numbering. It reads the folder's current order, the same `(name.lower(), name)` order every other mode uses:

```
file102.tif          ->  newname-001.tif
file105.tif          ->  newname-002.tif
file105b.tif         ->  newname-002b.tif
file105b-back.tif    ->  newname-002b-back.tif
```

That is the whole feature: "clean up and rename the files in this folder using this prefix." A `-` always separates the prefix from the number, so `parse_media_filename` reads a renamed file back exactly the way it read the original.

**Preview, then `-w`, exactly like the normal run.** `photokin ./scans --rename "newname"` alone plans the rename, prints the preview, and touches nothing — the same "check it's wired up" shape as a bare analysis run. Nothing is renamed until you add `-w`:

```
photokin ./scans --rename "newname"                       preview, touches nothing
photokin ./scans --rename "newname" -w                    record the plan and apply it
```

The prefix can be a template, not just a literal string — `{date}`, `{today}`, `{folder}` and `{orig}` tokens, each with an optional `:FORMAT` on the two date ones:

```
photokin ./scans --rename "{date:yymmdd}-bag" -w          520601-bag-001.tif; numbering restarts per date
photokin ./scans --rename "{today:yymmdd}-bag" -w         batch date (the run date) instead of each photo's own
photokin ./scans --rename "newname{date:yyyy-mm-dd}" -w   newname1952-06-01-001.tif
photokin ./scans --rename "{orig}" -w                     keep the current prefix, just renumber and clean up
```

A prefix that renders differently per file (any template using `{date}`) starts its numbering over at 1 for each distinct rendered value, so a folder spanning several scanning sessions gets one clean sequence per session rather than one long one. Companions sharing an image's stem (`.md`, `.json`, `.xmp`, `.txt`, plus a `.jpg` twin of a `.tif`) are carried along automatically; `--companions EXT[,EXT]` adds more extensions to that set.

**Undo.** Every apply writes a journal beside the renamed files before it renames anything, so `photokin ./scans --rename-undo` reverses the most recent applied run in that folder — or pass a journal path directly for an older one. An interrupted run resumes forward with `--rename-resume` instead of undoing; both read the journal back rather than re-planning. An undo that can only reverse part of a run leaves its journal open and says what is left, so running it again picks up the remainder rather than refusing as already undone. A journal path that is only a symlink is resolved before it is read, so the undo (or resume) acts on the folder its records actually describe — the linked-to journal's own folder — never on the folder holding the link.

**No destination doubles as a source.** `--plan-out` and the changeset `-w` writes are checked against everything else the run touches — every photo and companion it would rename, a file it reports left behind, an earlier run's journal in that folder, the manifest it read, and the names it is about to rename onto — and refused (exit 2, naming both the destination and what it turned out to be) rather than silently overwritten. That check runs as part of planning, before the plan file or the changeset is opened, so it applies to a bare preview exactly as it does under `-w`, and under `--dry-run` too.

**Photokin renames files on disk only with `-w`.** A folder tracked by a catalog application (Lightroom and the like) must be renamed through that application, not through photokin directly — photokin cannot tell such a folder apart from an ordinary one on its own, so every `--rename` preview says so. When a manifest was exported *by* that application (it carries `managed_by`), `-w` becomes a usage error rather than a guess: photokin plans the rename and, with `--plan-out PATH`, writes it out for the application to apply.

See [`docs/rename-mode.md`](https://github.com/asielen/photokin/blob/v0.6.0/docs/rename-mode.md) for the full specification and [`docs/rename-contract.md`](https://github.com/asielen/photokin/blob/v0.6.0/docs/rename-contract.md) for the manifest, plan and changeset shapes a wrapper reads.