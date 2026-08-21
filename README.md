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

Pick a folder to hold your virtual environment and any manifest/output files. This does *not* need to contain your actual photos — you'll point photokin at wherever those already live, by full path.

```powershell
mkdir C:\Users\YourName\photokin-work
cd C:\Users\YourName\photokin-work
```

One thing does not land here by default: a changeset is written beside the *input*, so `--changeset true` on a photo folder drops the `.ndjson` inside that folder. Pass `--output-file` a path in this folder and the changeset follows it.

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

Only needed if you want photokin to write metadata into the image files themselves:

```powershell
python -m photokin.exiftool.fetch
```

This downloads the official ExifTool binary into `~/.photokin/bin` — no separate system install required.

Installing it does not turn writing on. Nothing is written to your files unless you add `-w` to a run, so the commands in step 7 still only read. See [Setting up ExifTool](#setting-up-exiftool) for what `-w` writes and where.

### 7. Run it

Give it the full path to the photo — you're in `photokin-work`, not in your pictures folder, and the type is detected from the path you pass:

```powershell
photokin C:\Users\YourName\Pictures\scan_042.jpg --back C:\Users\YourName\Pictures\scan_042_back.jpg --provider anthropic
```

or against a whole folder:

```powershell
photokin C:\Users\YourName\Pictures\Scans\ --provider anthropic > results.json
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

Pick a folder to hold your virtual environment and any manifest/output files. This does *not* need to contain your actual photos — you'll point photokin at wherever those already live, by full path.

```bash
mkdir ~/photokin-work
cd ~/photokin-work
```

One thing does not land here by default: a changeset is written beside the *input*, so `--changeset true` on a photo folder drops the `.ndjson` inside that folder. Pass `--output-file` a path in this folder and the changeset follows it.

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

Only needed if you want photokin to write metadata into the image files themselves. Install it with Homebrew:

```bash
brew install exiftool
```

No Homebrew? Grab the installer package from exiftool.org instead. (`python -m photokin.exiftool.fetch` is a Windows-only convenience and does nothing here.)

Installing it does not turn writing on. Nothing is written to your files unless you add `-w` to a run, so the commands in step 7 still only read. See [Setting up ExifTool](#setting-up-exiftool) for what `-w` writes and where.

### 7. Run it

Give it the full path to the photo — you're in `photokin-work`, not in your pictures folder, and the type is detected from the path you pass:

```bash
photokin ~/Pictures/scan_042.jpg --back ~/Pictures/scan_042_back.jpg --provider anthropic
```

or against a whole folder:

```bash
photokin ~/Pictures/Scans/ --provider anthropic > results.json
```

### Coming back later

Each new session, just reactivate the environment before running photokin:

```bash
cd ~/photokin-work
source .venv/bin/activate
photokin ...
```

## Folders and batches

**The normal run is `-rw`.** Two commands cover almost everything:

```
photokin C:\Users\YourName\Pictures\Scans\ -rw
photokin box3_017.jpg --back box3_017-back.jpg -rw
```

That is: group every scan of one physical object together, read the metadata those files already hold, analyze each object once, and write the result back to **every** file in the group. All three of those are what you get by default — `--group-by object` is the default granularity, and the write reaches each group member rather than just the front. On a group of five files (a print, its back, a crop, a rescan and *its* back) one model call produces five sets of proposed writes, identical except that the `back` keyword lands only on the two backs and `negative` only on a negative.

The `-r` half is what makes the run *safe* rather than merely informed, for the reason spelled out under [Setting up ExifTool](#setting-up-exiftool): the date-correction rule can only protect a date it has read, so `-w` without `-r` lets a mediocre guess overwrite a good value.

**The run with no flags at all still works, and now tells you this.** `photokin C:\Scans` analyzes, prints the JSON to your terminal and touches nothing — that is the "check it's wired up" run and it is not going away. Its plan summary just ends with one extra row naming the next step, built from the command you actually typed so you can paste it straight back:

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

Whatever else you typed comes along — a `--provider`, a `--back`, a `--group-by` — so the suggested command is the run you just planned plus the two flags, not a different one. It appears only when you have said nothing either way: `-r`, `-w`, `--dry-run`, `--changeset`, `--exiftool-write`, `--output-file`, `--output-sidecars` and `--generate-manifest` each mean you have already decided, and the row stays quiet. (The quoting is real, not decoration: a path is wrapped in `"` when any shell would otherwise split or expand it, and a trailing `\` is doubled because a lone one would escape the closing quote. Paths holding `$`, `` ` ``, `%`, `!` or `"` get no suggestion at all rather than a wrong one.)

Point it at a folder and it works through everything in it (non-recursive). Filename suffixes group scans of the same physical object automatically — `photo-a.jpg` / `photo-b.jpg` are variants, `photo-back.jpg` is the reverse side, `album-page1.jpg` / `album-page2.jpg` are pages of one document — and each group is analyzed together as one object.

Folder and manifest input are read by the same grouper, so anything one handles the other handles identically. A set with no plain front scan in it is analyzed like any other object — nothing but pages (`album-page1.jpg`, `album-page2.jpg`), nothing but a negative, or nothing but a back (`box3_030-back.jpg`, where the front was never scanned or lives in another folder). Every page of a document goes to the model in one call, and every scan of a print goes in one call with its siblings. Albums, multi-page documents, negative-only scans and loose backs no longer need a manifest. (Earlier releases grouped those sets correctly and then skipped them, warning per group; that limitation is gone.)

**How much is one object: `--group-by {object,pair,none}`.** Granularity is a single axis, defaulting to `object`. On `box3_025.jpg`, `box3_025-back.jpg`, `box3_025b.jpg`, `box3_025b-back.jpg`, `box3_025c.jpg`:

| Value | Group key | On that five-file set | What each call sees |
|---|---|---|---|
| `object` (default) | the print | 1 call, 5 images; every scan shares one analysis | every image of the object, and the metadata read from all of them |
| `pair` | the print plus the variant letter | 3 calls, 5 images; each rescan judged on its own merits | one front with its own back; other variants invisible to it |
| `none` | the file | 5 calls, 5 images; every file alone, backs separated from fronts | one image and its own metadata, no other context at all |

Writes go to **every** file in the group at all three settings — the granularity decides what is analyzed together, never who gets written to.

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

Add `-r` and the document also captures what ExifTool read out of the files. A plain replay (`photokin scans-manifest.json`) then launches ExifTool not at all — every value it needs is already in the document.

Replaying *with* `-r` is what reproduces the original result exactly, because the document records the values but not that they came out of a file, and the title rule below turns on precisely that distinction. That costs a little more than nothing: the pre-flight insists an ExifTool binary exists, and `-r` re-reads any file whose recorded metadata is missing even one of the five tags — which is the normal case, since most files do not carry all five. Only a file that held the complete set replays without a subprocess.

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

## Captions

The caption is the one field where photokin has to reconcile what you already wrote with what the model just said, and where it writes the same text into several files at once. This section is what it does and why.

### The shape

Take a print you scanned twice, and scanned the back of the second time — `box3_017.jpg`, `box3_017b.jpg`, `box3_017b-back.jpg` — where each file already carries its own caption. Every one of those three files comes out holding this:

```
[Photo A] Caption A
[Photo B] Caption B
[Back] Back of Photo B
[AI Analysis]: Two people outside a bakery.
```

Not a third of it each: the whole thing, byte for byte, in all three. That is the point of it. Those three files are one physical photograph, and which of them you happen to open a year from now is an accident of how you were browsing. Opening any one of them should tell you the whole story of the object — that there were two scans, that one of them has writing on the back, and what that writing says — rather than the fragment that particular file was scanned with.

### The labels

A label is only added when it distinguishes something, and the variant letter is decided for each role separately. Three cases cover nearly everything:

**Two photos and one back.** The photos need telling apart, the back does not, so the back is bare:

```
[Photo A] Caption A
[Photo B] Caption B
[Back] Back of Photo B
```

**One photo and its back.** There is only one of each, so neither carries a letter:

```
[Photo] Ruth and Sam
[Back] pencil note
```

**A lone scan with no back.** Nothing to tell apart, so nothing is labelled and your caption is left exactly as you typed it:

```
Grandma on the porch
[AI Analysis]: Two people outside a bakery.
```

That last case is the overwhelmingly common one, and it is deliberately left alone — an archive of loose prints with no variants in it never grows a single bracket.

The letters are the letters on disk. `box3_017b.jpg` is `[Photo B]` because the file says `b`. The bare `box3_017.jpg` beside it is `[Photo A]`, because that is what it is: a bare scan is variant A, which is precisely why the *second* scan of a print is lettered `b` and not `a`. With no lettered sibling in the group there is nothing to disambiguate, so no letter is invented — a print and its own crop both come out as `[Photo]`.

### How it is built, and the surprise in it

The block is assembled once for the whole group and then written to every file in it. Concretely: photokin reads each file's existing caption while it still knows which file it came off — that is the only moment the attribution is free — labels it accordingly, merges the labelled pieces from across the group into one block, appends this run's analysis, and hands the same result to every member.

"Existing caption" means whatever the run was given: the `XMP:Description` in the file itself under `-r`, or a `caption` you put in a manifest item's `metadata`. Without either, there is nothing to merge and the block is just the analysis line — which is another reason the normal run is `-rw`.

**This means a caption you typed on one file will appear on its siblings.** If you wrote "Ruth and Sam outside the bakery" on the front scan only, after a run the back scan holds it too, as `[Photo] Ruth and Sam outside the bakery`. That is intended and it is the whole feature, but it will surprise you the first time, so: if you do not want two scans sharing captions, they are not one object as far as photokin is concerned — put them in different groups, or run with `--group-by none`, which analyses and captions every file entirely on its own.

The order of the block is the group's own order — photos before backs, variant A before variant B — and never the order your files happened to be listed in, so the same folder produces the same block on every run and on every machine.

### What happens to a caption you already have

Nothing you wrote is ever deleted. Beyond that there are three cases, and they are decided per section rather than on the caption as a whole:

- **Identical, or near enough.** Nothing changes. Two files of a group very often hold the same caption typed twice, and photokin writes it once instead of once per file.
- **Materially different.** The existing caption is kept and the new content is added beside it, each under its own label.
- **A partial version of the block.** Say a file already holds `[Photo A]` from an earlier run and the group has since gained a second scan. The `[Photo B]` line is filled in and the `[Photo A]` line is left exactly as it is.

That third case is the reason any of this is labelled. Merging happens **per section, never whole-string** — and the labels are what make a section a thing that can be found at all. Each labelled section is settled on its own text, so a change in one cannot disturb another. Compare a whole-string approach, which would find old and new unequal and then have to choose between appending the entire old block again or overwriting it — either way, touching lines it had no business touching.

**"Near enough" means punctuation, spacing, quoting and capitalisation.** A trailing full stop, a curly apostrophe against a straight one, an em dash for a hyphen, a stray inner comma — those are one caption typed twice, and the second is dropped. **Anything that changes a word is kept**, including changes that look tiny: `bakery, 1948` against `bakery, 1949` is a different caption, and so is `Ruth and Sam` against `Ruth and Edith`. If you reword a caption and want the old one gone, delete it yourself; photokin will not guess that a rewrite was meant to replace rather than accompany, because guessing wrong there loses something you cannot get back.

For the curious, the deciding comparison is on the words: two captions with the same word sequence are one caption however they are punctuated. A `difflib` similarity ratio of 0.998 sits behind that as a second gate, for a difference too small to be a word — a stray character in a long block. It is set that high on purpose. Measured against real caption pairs, no ratio can separate cosmetic from material on its own: a changed *year* inside a 300-character analysis scores 0.9967, while a changed *quote mark* in a short caption scores 0.9091, so any threshold loose enough to catch the second would throw away the first. 0.998 clears every material difference measured, which is what makes it structurally unable to be the thing that loses your correction.

### No second model call

None of the merging above costs an API call. The structural part is deterministic string work: the block is labelled, so it is keyed, so merging it is a matter of matching sections rather than of judgement.

The judgement that genuinely needs a model — whether two differently worded captions mean the same thing, and which parts of an existing caption are worth keeping — already happens in the analysis call you are already paying for. With `-r`, the caption a file already holds is forwarded to the model as context, and `photokin/prompts_photo_ai/instructions_front_back.txt:261-276` instructs it to evaluate that caption before writing: preserve unique human context (names, events, places, dates, relationships), feel free to replace text that merely re-describes the image or that an earlier run generated, and return a merged whole. So the semantic decision is made once, in the call that was already going to happen.

### Running it twice does not grow your captions

Under `-rw`, the block photokin writes into a file is exactly what the next run reads back out of it. That is a real trap — an earlier release of photokin appended another copy of the caption on every pass — so it is now a property the test suite pins directly: run `-rw` three times over the same folder and the caption is byte-identical after the first run.

Two things make that true. Labelled lines are recognised as photokin's own output and taken as they are, never labelled a second time. And the `[AI Analysis]` section is regenerated rather than accumulated: everything from that marker to the end of the caption is the previous run's analysis, so a model that rewords itself between runs replaces its old paragraph instead of adding a second one. Everything above the marker is yours and is never touched.

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

ExifTool is optional, and can be both used for reading initial values from the photos (so you don't just overwrite everything) and also write to the photos after. With `-r`, it reads `EXIF:DateTimeOriginal`, `EXIF:UserComment`, `XMP:Description`, `XMP:Title` and `XMP:Subject` straight out of the files before analysis, so a note, a caption, a title, a date or a keyword already living in an image rides along to the model as context (hydration). After analysis, `-w` writes approved changeset fields back into the files or their sidecars (apply).

The two halves mirror each other: both are explicit opt-ins, neither implies the other, and both work for folder, manifest and single-photo input alike. `-r` fills only what the input does not already carry, so a value from a manifest item or from `--meta` is never overridden, and it writes nothing anywhere. Reading the whole set rather than one tag is deliberate — knowing what a file already holds is how the run knows what *not* to change. Every run's plan summary names the read set, so `read : none (-r not given)` is visible before the first model call.

`-r` is worth knowing about for two non-obvious reasons. The first is dates. The date-correction heuristic compares the file's own `EXIF:DateTimeOriginal` against the model's inference and rewrites it only when they disagree by a wide margin — the rule that fixes a 2019 scan date on a 1952 print while leaving a modern photo alone. Without a date read out of the file there is nothing to compare against, so in folder mode that heuristic never fired at all. `-r` is what makes it live. The file's date is treated as evidence rather than as truth: it drives that comparison and fills `dateTimeOriginal` when the comparison declines, but it no longer overwrites the model's `date_guess`, which on a flatbed scan would assert the day you scanned the print as the day the photograph was taken.

The second is titles. Scanner software routinely writes "Scanned Image" or the bare filename into `XMP:Title`, so a title read out of a file is not the same evidence as a title you typed — under `-r`, a title the model transcribed off the print wins, and the file's own is kept only when the model returned none. A title supplied by a manifest item or by `--meta` is unaffected and still beats the model outright: a human wrote that one, and letting a transcription overwrite it would lose data rather than junk. The distinction is provenance, not content — the same string wins or loses depending on where it came from.

Where each result field lands when written:

| Result field | Tag |
|---|---|
| `ai_caption` (the AI analysis) | `EXIF:UserComment` |
| `caption` (the verbatim transcription) | `XMP-dc:Description` |
| `keywords` | `XMP-dc:Subject` |
| `title` | `XMP-dc:Title` |
| `date_guess` (when confident enough) | `EXIF:DateTimeOriginal` |
| `location_guess` (when confident enough) | `IPTC:Country-PrimaryLocationName` / `Province-State` / `City` / `Sub-location` |

Those are the spellings to pass to `--exiftool-fields`, verbatim. The XMP ones are hyphenated (`XMP-dc:Description`) because that is the form ExifTool writes and the form it prints back under `-G1`; the colon form `XMP:dc:Description` is **not** writable — ExifTool answers "doesn't exist or isn't writable" and writes nothing. Earlier versions of photokin used the colon form internally, so a command copied from an older changeset or an older copy of these docs will name it; a run that does is now stopped before its first model call with the correct spelling quoted, rather than analysing the whole batch and writing none of it. Note that reading is more forgiving than writing — `-r` asks for the bare `XMP:Description`, which resolves to the same tag.

**Setup.** On Windows, run `python -m photokin.exiftool.fetch` once — it downloads the official ExifTool distribution from the project's SourceForge host and verifies it against the SHA256 exiftool.org publishes, into `~/.photokin/bin`, no system install needed. On macOS/Linux install it yourself: `brew install exiftool` or `apt install libimage-exiftool-perl`. At runtime the binary is found in this order: an explicit `--exiftool-path` / `EXIFTOOL_PATH`, then the downloaded copy in `~/.photokin/bin`, then whatever `exiftool` is on your `PATH`.

If none is found, a run that asked for one stops before it costs anything: `-r` and `-w` both resolve the binary before the first model call, so either flag with no ExifTool anywhere exits 2 immediately rather than analyzing the whole batch and only then discovering it cannot read or write any of it. Once that check passes, a mid-run failure on a single file — a lock, a corrupt image, a timeout — is a warning on both sides and the analysis still runs.

**Writing during a run.** Nothing is written into your files unless you ask for it: `--exiftool-write` defaults to `false`, and a changeset on its own only records what *would* be written. `-w` is the one-flag spelling of `--changeset true --exiftool-write true` — record the proposed writes and apply them — and works for folder, manifest and single-photo input alike. Spelled out, that is `--changeset true --exiftool-write true` — those two flags and no others, with `--exiftool-fields` left at its `EXIF:UserComment` default; `--exiftool-write true` is required rather than a confirmation of the default. The same settings are available as env vars (`EXIFTOOL_WRITE_ENABLED`, `EXIFTOOL_FIELDS`, `EXIFTOOL_PATH`), with flags winning over env over defaults. Every run prints a plan summary before its first model call naming the write set, so "nothing will be written" is visible up front; `--dry-run` prints that summary and stops.

**When writes fail.** A run that asked to write, saw files, and wrote *none* of them exits 2 and says so — that shape is always a setting that is wrong for every file (an unwritable `--exiftool-fields` tag, a read-only folder, a binary that will not run), so a script moving on to the next box of scans should stop. A *partial* failure exits 0: some files were written, so the settings were right, and one locked or corrupt file among many is ordinary. Either way the per-file reasons are logged as `[ExifTool] Errors:` before the run ends. Manifest mode is the exception and always exits 0, because it is the Lightroom plug-in's contract and the plug-in reads the per-item records rather than the exit status.

**Use `-rw`, not `-w`.** The two short flags combine the way any short flags do, so `photokin C:\Scans\ -rw` is the whole normal run: read what the files already hold, then write the results back. Prefer it over bare `-w`, which is genuinely the more dangerous of the two. The date-correction heuristic can only protect a date by *comparing* against it, so with nothing read there is nothing to compare and a mediocre guess overwrites a good value. Measured, on a modern photo already carrying a correct `2019:08:14` against a model guessing 2005 at confidence 0.72:

```
photokin <folder> -w     proposes  EXIF:DateTimeOriginal = 2005-06-15    # overwrites a correct date
photokin <folder> -rw    proposes  nothing                               # 14-year gap is under the threshold
```

`-w` alone is still the right flag when the files genuinely hold nothing worth reading — a folder of fresh scans straight off the scanner, where every read would come back empty and `-r` only costs you a subprocess.

**Know where it lands before you start a batch.** Both halves of `-w` reach into your photo directory. The changeset is written beside the *input* unless `--output-file` redirects it, so `photokin ./scans/ -w` drops `scans_changeset.ndjson` **inside `./scans/`** and then edits the images there in place. Photo directories are often cloud-synced, network-mounted or read-only, and none of those is a good place to discover this: give `--output-file` a path somewhere you control and the changeset follows it, or run `--dry-run` first, which prints the exact changeset path it would use and stops.

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
| `--date-confidence-threshold X`                    | Minimum model confidence before a date guess is written into a file that has no date, 0-1 (default 0.6). Replacing a date the file already holds is governed separately and costs more; see below |
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

### ExifTool read and write-back

| Flag                            | What it does |
|---------------------------------|---|
| `-r`, `--read`                  | Before analysis, read `EXIF:DateTimeOriginal`, `EXIF:UserComment`, `XMP:Description`, `XMP:Title` and `XMP:Subject` out of the files and send them to the model, for every input type. Only fills what the input does not already carry; nothing is written. Mirrors `-w` |
| `-w`, `--write`                 | Shorthand for `--changeset true --exiftool-write true`: record the proposed writes and apply them. An explicit flag that contradicts it is an error rather than a guess |
| `--exiftool-write {true,false}` | Apply changeset fields to the files after analysis (default `false`; nothing is written without an explicit opt-in) |
| `--exiftool-fields TAGS`        | Comma-separated tags ExifTool may write (default `EXIF:UserComment`) |
| `--exiftool-path PATH`          | ExifTool binary to use (default: auto-detect) |

`-r` is the read half and `-w` the write half; the short letters are deliberately symmetrical, and they combine as `-rw` (or `-wr`) exactly like any other pair of short flags — that combined form is the one to reach for, for the reason given above. `-R` is **reserved** for the recursive-folder flag that is still deferred (it changes grouping semantics across directories and interacts with write safety, so it gets its own change), and must not be spent on anything else.

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
