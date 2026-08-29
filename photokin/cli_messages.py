"""
photokin.cli_messages
=====================

The wording of every user-facing CLI message, and nothing else.

``cli.py`` owns *when* a message fires and *how* it is emitted; this module owns
what it says. Every function here is pure: no logging, no I/O, no ``sys.exit``,
and no import beyond the standard library's ``dataclasses``. In particular it
does not import ``photokin.core`` or ``photokin.utils``, so the wording can be
tested without paying for PIL and the provider SDKs.

Errors are returned as a :data:`UsageMessage` -- a ``(problem, remedy)`` pair
that ``cli._exit_with_usage_error`` renders as ``"<problem>\\nTry: <remedy>"``.
Neither half may contain a newline, so the rendered block is always exactly two
lines. The problem line leads with its subject in backticks and ends with a
period; the remedy line carries no trailing punctuation. The five
:data:`_VERBATIM_FROM_PHASE_A` functions predate that style and are kept
character-for-character, because their text is pinned by existing tests.

Code map:
- UsageMessage      the ``(problem, remedy)`` pair every error returns
- input_*/folder_*/json_*/manifest_*  detection and content validation
- positional_and_alias, two_aliases, no_input_given, alias_*  input selection
- flag_*, write_*, output_file_extension                      flag combinations
- generate_manifest_*                                         --generate-manifest
- output_*, exiftool_not_found, exiftool_field_is_not_writable  pre-flight (verbatim)
- detected_as, exiftool_fields_with_no_write                  notes, not errors
- normal_run_command  PUBLIC: the ``-rw`` run to advise, rebuilt from the argv
- RunPlan           PUBLIC: the plan summary printed before the first model call
- rename_*                                                    --rename and its executor commands
- render_rename_preview     PUBLIC: the rename plan's default table (6.2, 5.6)
- render_rename_run_report  PUBLIC: an apply/undo/resume/finish run's outcome
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

#: A problem line and the remedy line that follows it, in that order.
UsageMessage = tuple[str, str]

#: Messages moved unchanged out of ``cli.py`` when the wording was centralized.
#: Their problem lines predate the backticks-first house style and do not end in
#: a period; existing tests pin them, so a style sweep would be a behavior
#: change. Anything asserting the house style over this module must exempt them.
_VERBATIM_FROM_PHASE_A: frozenset[str] = frozenset(
    {
        "exiftool_not_found",
        "output_destination_not_writable",
        "output_dir_missing",
        "output_is_a_directory",
        "output_not_writable",
    }
)

#: The three spellings named whenever the CLI has to show what an input looks
#: like. One string, so the examples cannot drift apart between messages.
_INPUT_EXAMPLES = "photokin ./scans/, photokin batch.json, or photokin scan_042.jpg"

#: How a manifest is rebuilt, quoted by every message about a broken one.
_REBUILD_HINT = "photokin ./scans/ --generate-manifest batch.json"


def _one_line(text: str) -> str:
    """Collapse *text* onto a single line so a two-line block stays two lines.

    Args:
        text: Free text from outside the CLI -- an exception message, an OS
            error string -- which may carry newlines the format cannot hold.

    Returns:
        The same text with every run of whitespace reduced to one space.
    """
    return " ".join(text.split())


# === Input detection ===

def input_not_found(display: str) -> UsageMessage:
    """The input path does not exist, not even as a dangling symlink.

    Args:
        display: The path exactly as the user typed it, quoted back in backticks.

    Returns:
        The problem line and the remedy line, in that order.
    """
    return (
        f"`{display}` not found.",
        "check the spelling, or run from the folder that contains it",
    )


def input_names_nothing(display: str) -> UsageMessage:
    """The input token is blank, so it addresses no path at all.

    Args:
        display: The token exactly as the user typed it, quoted back so the
            blankness is visible.

    Returns:
        The problem line and the remedy line, in that order.
    """
    return (
        f"`{display}` is blank, so it names no input.",
        f"name one: {_INPUT_EXAMPLES}",
    )


def input_is_a_broken_symlink(display: str, target: str) -> UsageMessage:
    """The input is a symlink that resolves to nothing.

    Args:
        display: The path exactly as the user typed it.
        target: What the link points at, as ``os.readlink`` reports it.

    Returns:
        The problem line and the remedy line, in that order.
    """
    return (
        f"`{display}` is a symlink whose target does not exist: {target}.",
        "repoint the link, or pass the real path",
    )


def input_is_not_a_file_or_folder(display: str) -> UsageMessage:
    """The input exists but is a FIFO, a device, or a socket.

    Args:
        display: The path exactly as the user typed it.

    Returns:
        The problem line and the remedy line, in that order.
    """
    return (
        f"`{display}` exists but is neither a file nor a folder.",
        "pass an image, a folder of images, or a .json manifest",
    )


def folder_has_no_images(display: str, extensions: str) -> UsageMessage:
    """A folder input holds nothing the pipeline can read.

    Args:
        display: The folder exactly as the user typed it.
        extensions: The rendered list of extensions that were looked for.

    Returns:
        The problem line and the remedy line, in that order.
    """
    return (
        f"`{display}` holds no images; looked for {extensions}.",
        "point at the folder that holds the scans, or convert the files to one "
        "of those formats",
    )


def folder_cannot_be_read(display: str, reason: str) -> UsageMessage:
    """A folder input exists but cannot be listed.

    Args:
        display: The folder exactly as the user typed it.
        reason: The operating system's explanation.

    Returns:
        The problem line and the remedy line, in that order.
    """
    return (
        f"`{display}` cannot be read: {_one_line(reason)}.",
        "check the folder's permissions, or point at one you can read",
    )


def manifest_cannot_be_read(display: str, reason: str) -> UsageMessage:
    """A manifest input exists but cannot be opened.

    The counterpart of :func:`folder_cannot_be_read`. Detection has already
    proved the file is there, so "not found" would contradict the line above it
    and send the reader off checking a spelling that is correct.

    Args:
        display: The manifest exactly as the user typed it.
        reason: The operating system's explanation.

    Returns:
        The problem line and the remedy line, in that order.
    """
    return (
        f"`{display}` cannot be read: {_one_line(reason)}.",
        "check the file's permissions, or close whatever is holding it open",
    )


def json_is_unreadable(display: str, reason: str) -> UsageMessage:
    """A ``.json`` input cannot be parsed at all.

    Args:
        display: The file exactly as the user typed it.
        reason: The decoder's own explanation.

    Returns:
        The problem line and the remedy line, in that order.
    """
    return (
        f"`{display}` is not valid JSON: {_one_line(reason)}.",
        f"fix the JSON, or rebuild it: {_REBUILD_HINT}",
    )


def json_is_not_a_manifest(display: str) -> UsageMessage:
    """A ``.json`` input parses but is not shaped like a manifest.

    Args:
        display: The file exactly as the user typed it.

    Returns:
        The problem line and the remedy line, in that order.
    """
    return (
        f"`{display}` is not a manifest; expected a top-level `items` list.",
        f"build one from a folder: {_REBUILD_HINT}",
    )


def manifest_has_no_items(display: str) -> UsageMessage:
    """A manifest carries an ``items`` list with nothing in it.

    Args:
        display: The manifest exactly as the user typed it.

    Returns:
        The problem line and the remedy line, in that order.
    """
    return (
        f"`{display}` has an empty `items` list; there is nothing to analyze.",
        f"add items, or rebuild it: {_REBUILD_HINT}",
    )


def manifest_item_has_no_path(display: str, index: int) -> UsageMessage:
    """One manifest item carries no usable ``path`` string.

    Args:
        display: The manifest exactly as the user typed it.
        index: The item's 0-based position, rendered the way the JSON is
            addressed.

    Returns:
        The problem line and the remedy line, in that order.
    """
    return (
        f"`{display}` items[{index}] has no `path` string.",
        "give every item a `path`, or rebuild the manifest with --generate-manifest",
    )


def manifest_item_not_found(display: str, index: int, item_path: str) -> UsageMessage:
    """One manifest item names a file that is not there.

    Args:
        display: The manifest exactly as the user typed it.
        index: The item's 0-based position.
        item_path: The path the item names, as the manifest spells it.

    Returns:
        The problem line and the remedy line, in that order.
    """
    return (
        f"`{display}` items[{index}] points at a file that does not exist: {item_path}.",
        "fix that path, or rebuild the manifest with --generate-manifest",
    )


def unrecognized_input_extension(display: str) -> UsageMessage:
    """The input is a regular file the pipeline has no reading for.

    Args:
        display: The path exactly as the user typed it.

    Returns:
        The problem line and the remedy line, in that order.
    """
    return (
        f"`{display}` isn't an image, folder, or .json manifest.",
        "photokin ./scans/ (folder), photokin batch.json (manifest), "
        "or photokin scan_042.jpg (single photo)",
    )


# === Input selection ===

def positional_and_alias(positional: str, alias_flag: str, alias_value: str) -> UsageMessage:
    """Both a positional input and one of its aliases were given.

    Args:
        positional: The positional token, exactly as typed.
        alias_flag: ``--folder`` or ``--manifest``.
        alias_value: The value that alias carried.

    Returns:
        The problem line and the remedy line, in that order.
    """
    return (
        f"`{positional}` was given as the input and `{alias_flag} {alias_value}` "
        "names another; only one input is allowed.",
        f"pass just one: photokin {positional}, or photokin {alias_flag} {alias_value}",
    )


def two_aliases(folder_value: str, manifest_value: str) -> UsageMessage:
    """``--folder`` and ``--manifest`` were both given.

    Args:
        folder_value: The value ``--folder`` carried.
        manifest_value: The value ``--manifest`` carried.

    Returns:
        The problem line and the remedy line, in that order.
    """
    return (
        f"`--folder {folder_value}` and `--manifest {manifest_value}` both name "
        "an input; only one is allowed.",
        f"pass just one: photokin {folder_value}, or photokin {manifest_value}",
    )


def no_input_given() -> UsageMessage:
    """Arguments were passed, but none of them names an input.

    Returns:
        The problem line and the remedy line, in that order.
    """
    return ("no input was given.", f"name one: {_INPUT_EXAMPLES}")


def no_input_and_not_interactive() -> UsageMessage:
    """No arguments at all, and stdin is not a terminal to prompt on.

    The interactive prompt is a courtesy for a human at a keyboard; a
    subprocess launcher (a plugin, a script, a scheduled task) with a mangled
    or empty argument list is not that, and blocking it on a stdin read it
    can never answer is worse than a usage error it can log and act on.

    Returns:
        The problem line and the remedy line, in that order.
    """
    return (
        "no input was given, and there is no terminal to prompt on.",
        f"name one: {_INPUT_EXAMPLES}",
    )


def alias_is_not_a_directory(value: str) -> UsageMessage:
    """``--folder`` asserts a directory, and the path is not one.

    Args:
        value: The value ``--folder`` carried.

    Returns:
        The problem line and the remedy line, in that order.
    """
    return (
        f"`--folder {value}` is not a folder.",
        "name a directory, or drop --folder and let the type be detected: "
        f"photokin {value}",
    )


def alias_is_not_a_json_file(value: str) -> UsageMessage:
    """``--manifest`` asserts a ``.json`` file, and the path is not one.

    Args:
        value: The value ``--manifest`` carried.

    Returns:
        The problem line and the remedy line, in that order.
    """
    return (
        f"`--manifest {value}` is not a .json manifest file.",
        "name the manifest file, or drop --manifest and let the type be detected: "
        f"photokin {value}",
    )


def flag_path_not_found(flag: str, value: str) -> UsageMessage:
    """A path-valued flag names a file that is not there.

    Args:
        flag: The flag, such as ``--back``.
        value: The path it carried, exactly as typed.

    Returns:
        The problem line and the remedy line, in that order.
    """
    return (
        f"`{flag} {value}` not found.",
        f"check the spelling, or drop {flag}",
    )


# === Flag combinations ===

def flag_needs_single_photo_input(
    flag: str, value: str, input_display: str, input_kind: str
) -> UsageMessage:
    """A single-photo flag was given beside folder or manifest input.

    Args:
        flag: ``--back`` or ``--meta``.
        value: The path that flag carried.
        input_display: The input, exactly as the user typed it.
        input_kind: What the input was taken to be -- ``"a folder"`` or
            ``"a manifest"`` -- so the message states the inference the fix
            depends on.

    Returns:
        The problem line and the remedy line, in that order.
    """
    problem = (
        f"`{flag} {value}` only applies to a single photo, but `{input_display}` "
        f"was treated as {input_kind}."
    )
    if flag == "--meta" and input_kind.endswith("manifest"):
        return (
            problem,
            "carry it in the manifest item's `metadata` or `metadata_path` instead",
        )
    return (problem, f"name the front image instead: photokin <front image> {flag} {value}")


def write_bundle_contradiction(flag: str, value: str) -> UsageMessage:
    """``-w`` was given beside a flag that contradicts what it expands to.

    Args:
        flag: The contradicting flag, ``--changeset`` or ``--exiftool-write``.
        value: The value it carried.

    Returns:
        The problem line and the remedy line, in that order.
    """
    return (
        "`-w` means --changeset true --exiftool-write true, but "
        f"`{flag} {value}` was also given.",
        "drop one; --changeset true alone records the proposed writes without "
        "applying them",
    )


def verbose_bundle_contradiction(flag: str, value: str) -> UsageMessage:
    """``-v`` was given beside a flag that contradicts what it expands to.

    Args:
        flag: The contradicting flag, ``--debug-dump-llm-request`` or
            ``--debug-dump-hydration``.
        value: The value it carried.

    Returns:
        The problem line and the remedy line, in that order.
    """
    return (
        "`-v` means --debug-dump-llm-request true --debug-dump-hydration true, "
        f"but `{flag} {value}` was also given.",
        "drop one, or set the flag to true to agree with -v",
    )


def write_needs_changeset() -> UsageMessage:
    """Writing was asked for with no changeset to write from.

    Returns:
        The problem line and the remedy line, in that order.
    """
    return (
        "`--exiftool-write true` needs a changeset to apply, but --changeset is false.",
        "add --changeset true, or use -w which sets both",
    )


def output_file_extension(value: str) -> UsageMessage:
    """``--output-file`` names an extension the run cannot write.

    Args:
        value: The path the flag carried.

    Returns:
        The problem line and the remedy line, in that order.
    """
    return (
        f"`--output-file {value}` must end with .ndjson or .json.",
        "use .ndjson to stream one record per finished photo, or .json for a "
        "single object written at the end",
    )


# === --generate-manifest ===

def generate_manifest_with_manifest_input() -> UsageMessage:
    """The flag describes how input would be grouped; a manifest already does.

    Returns:
        The problem line and the remedy line, in that order.
    """
    return (
        "--generate-manifest turns a folder into a manifest, and this input "
        "is already one.",
        "drop --generate-manifest, or point it at a folder: "
        "photokin --folder ./scans/ --generate-manifest out.json",
    )


def generate_manifest_without_input(out_path: str) -> UsageMessage:
    """The flag was given with nothing to describe.

    Args:
        out_path: The destination the flag named.

    Returns:
        The problem line and the remedy line, in that order.
    """
    return (
        "--generate-manifest describes how an input would be grouped, but no "
        "folder or image was given.",
        f"name the input: photokin --folder ./scans/ --generate-manifest {out_path}",
    )


def generate_manifest_extension(out_path: str) -> UsageMessage:
    """The flag's destination is not a ``.json`` file.

    Args:
        out_path: The destination the flag named.

    Returns:
        The problem line and the remedy line, in that order.
    """
    return (
        f"--generate-manifest must end with .json; got {out_path}.",
        "name the file itself, such as scans-manifest.json",
    )


def generate_manifest_with_output_file(value: str) -> UsageMessage:
    """``--generate-manifest`` stops the run before results can be written.

    Args:
        value: The path ``--output-file`` carried.

    Returns:
        The problem line and the remedy line, in that order.
    """
    return (
        f"`--generate-manifest` writes a manifest and stops, so `--output-file "
        f"{value}` would never be written.",
        "drop one; generate first, then feed the file back: "
        f"photokin <manifest> --output-file {value}",
    )


def generate_manifest_with_write_flag(flag: str, verb: str, replay: str) -> UsageMessage:
    """``--generate-manifest`` makes no model call, so a write flag has no subject.

    Args:
        flag: The write flag as it was spelled, such as ``--changeset true``.
        verb: What that flag would have done -- ``"write"`` or ``"record"``.
        replay: How to ask for it once the manifest exists.

    Returns:
        The problem line and the remedy line, in that order.
    """
    return (
        f"`--generate-manifest` makes no model call, so `{flag}` has nothing to {verb}.",
        f"drop it; generate first, then: photokin <manifest> {replay}",
    )


# === Pre-flight, moved verbatim from Phase A ===

def output_dir_missing(role: str, out_dir: str) -> UsageMessage:
    """The destination's parent directory does not exist.

    Args:
        role: The flag the destination came from.
        out_dir: The directory that is missing.

    Returns:
        The problem line and the remedy line, in that order.
    """
    return (
        f"{role} directory does not exist: {out_dir}",
        "create the directory first, or point --output-file at an existing one",
    )


def output_is_a_directory(role: str, out_path: str) -> UsageMessage:
    """The destination names a directory rather than a file.

    Args:
        role: The flag the destination came from.
        out_path: The destination.

    Returns:
        The problem line and the remedy line, in that order.
    """
    return (
        f"{role} is a directory, not a file: {out_path}",
        "name the file itself, such as results.ndjson inside that directory",
    )


def output_not_writable(role: str, out_path: str) -> UsageMessage:
    """The destination exists and is read-only.

    Args:
        role: The flag the destination came from.
        out_path: The destination.

    Returns:
        The problem line and the remedy line, in that order.
    """
    return (
        f"{role} already exists and is not writable: {out_path}",
        "clear the read-only flag on that file, or choose a different --output-file",
    )


def output_destination_not_writable(role: str, out_path: str, reason: str) -> UsageMessage:
    """The destination directory refused a probe write.

    Args:
        role: The flag the destination came from.
        out_path: The destination.
        reason: The operating system's explanation.

    Returns:
        The problem line and the remedy line, in that order.
    """
    # Reason first, path last: the reason is short and is what the reader acts
    # on, while the path can be any length and so belongs at the end where it
    # cannot push anything off the line.
    return (
        f"{role} cannot be written -- {_one_line(reason)}:\n  {out_path}",
        "point --output-file at a writable directory",
    )


def invalid_llm_provider_env(value: str) -> UsageMessage:
    """``LLM_PROVIDER`` is set to a value no provider recognizes.

    A typo here (``LLM_PROVIDER=antropic``) would otherwise fall through
    ``normalize_provider``'s permissive default and run OpenAI silently --
    exactly the guess this whole resolution order exists to avoid.

    Args:
        value: The unrecognized ``LLM_PROVIDER`` value.

    Returns:
        The problem line and the remedy line, in that order.
    """
    return (
        f"LLM_PROVIDER=`{value}` is not a provider photokin knows.",
        "set it to one of openai, anthropic, gemini, openrouter, or unset it and pass --provider",
    )


def no_provider_sdk_installed() -> UsageMessage:
    """No --provider was given and no provider SDK is importable.

    Returns:
        The problem line and the remedy line, in that order.
    """
    return (
        "no provider SDK is installed, so there is no backend to call.",
        'install one: pip install "photokin[openai]" (or [anthropic] / [gemini])',
    )


def multiple_provider_sdks_installed(installed: list[str]) -> UsageMessage:
    """No --provider was given and more than one provider SDK is importable.

    The remedy names a real choice off the installed list rather than a
    placeholder, so it can be pasted as-is.

    Args:
        installed: The providers whose SDK is present, in detection order.

    Returns:
        The problem line and the remedy line, in that order.
    """
    return (
        f"more than one provider SDK is installed ({', '.join(installed)}), and the run won't guess which to call.",
        f"pick one per run with --provider {installed[0]}, or set LLM_PROVIDER once to make it the default",
    )


def exiftool_not_found(configured_path: str) -> UsageMessage:
    """Writes were requested and no ExifTool binary resolves.

    Args:
        configured_path: The path ``--exiftool-path``/``EXIFTOOL_PATH`` named,
            or an empty string when neither was set.

    Returns:
        The problem line and the remedy line, in that order.
    """
    # The configured path goes on its own line rather than into a parenthetical:
    # a Windows path is routinely 80+ characters and swallows the sentence it
    # sits inside, which is the one thing the reader needs to be able to skim.
    configured = f"\n  looked for it at: {configured_path}" if configured_path else ""
    return (
        f"no ExifTool found, and writing needs it.{configured}",
        "run `python -m photokin.exiftool.fetch` to install one, "
        "or drop -w to analyze without writing",
    )


def exiftool_not_found_for_read(configured_path: str) -> UsageMessage:
    """Reads were requested and no ExifTool binary resolves.

    A read that cannot run fails quietly -- the batch is analyzed and paid for
    with a strictly worse prompt -- so it is caught up front, exactly as a
    requested write is. Writes are reported first when both are broken, since
    their remedy fixes the read too.

    Args:
        configured_path: The path ``--exiftool-path``/``EXIFTOOL_PATH`` named,
            or an empty string when neither was set.

    Returns:
        The problem line and the remedy line, in that order.
    """
    # Same shape as exiftool_not_found, deliberately: a reader who has hit one
    # of these recognizes the other at a glance instead of reading it afresh.
    configured = f"\n  looked for it at: {configured_path}" if configured_path else ""
    return (
        f"no ExifTool found, and -r needs it to read your files.{configured}",
        "run `python -m photokin.exiftool.fetch` to install one, "
        "or drop -r to analyze without reading",
    )


def every_write_failed() -> UsageMessage:
    """Writes were requested, files were seen, and not one was written.

    Reported as a usage error rather than left to the exit status, because the
    run has otherwise succeeded loudly -- the analysis ran, the results printed,
    the changeset was written -- and the one thing the user asked for silently
    did not happen. Partial failure is deliberately not routed here: some files
    did get their metadata, and one locked or corrupt file is ordinary. Zero of
    many is not ordinary; every file failed for the same reason, and that reason
    is a setting rather than a photo.

    The per-file reasons are in the ``[ExifTool] Errors:`` record logged just
    above, which is why this line does not try to restate them.

    Returns:
        The problem line and the remedy line, in that order.
    """
    return (
        "nothing was written -- every file ExifTool tried failed.",
        "see the ExifTool errors above; the usual causes are an unwritable "
        "`--exiftool-fields` tag or a read-only folder",
    )


# === Notes, which are not errors ===

def detected_as(display: str, kind_label: str, reason: str) -> str:
    """State what a positional input was taken to be, and why.

    Args:
        display: The path exactly as the user typed it.
        kind_label: ``"a folder"``, ``"a manifest"`` or ``"a single photo"``.
        reason: The rule that decided it, such as ``"it is a directory"``.

    Returns:
        One line, ready to log.
    """
    return f"Treating `{display}` as {kind_label} ({reason})."


def exiftool_fields_with_no_write(value: str) -> str:
    """Note that ``--exiftool-fields`` was accepted but has nothing to act on.

    Args:
        value: The tag list the flag carried.

    Returns:
        One line, ready to log at WARNING.
    """
    return (
        f"`--exiftool-fields {value}` was given but nothing will be written; "
        "add -w (or --changeset true --exiftool-write true) to apply those tags."
    )


def exiftool_field_is_not_writable(bad: str, good: str) -> tuple[str, str]:
    """Reject a tag spelling ExifTool cannot write, naming the one it can.

    Caught before the first model call rather than at apply time on purpose.
    ExifTool's own answer to this spelling is "Sorry, ... doesn't exist or isn't
    writable" followed by "Nothing to do", reported once per file after the
    whole batch has been analysed and paid for -- so the run costs full price
    and writes nothing. One line up front is worth more than an accurate
    post-mortem.

    Args:
        bad: The tag exactly as the user typed it.
        good: The writable spelling for the same tag.

    Returns:
        A ``(problem, remedy)`` pair for :func:`_exit_with_usage_error`.
    """
    # What ExifTool would have said, and why the run would have cost full price
    # before saying it, are both in the docstring above rather than in the
    # message: the reader needs the tag and the spelling that works, and a
    # second clause explaining the mechanism only delays both.
    return (
        f"ExifTool cannot write `{bad}`.",
        f"use `{good}` instead -- the same tag, spelled the way ExifTool wants it",
    )


# === The normal run, rebuilt so it can be pasted ===

#: What a token may hold and still be pasted back bare. A whitelist on purpose:
#: cmd.exe, PowerShell and the POSIX shells disagree about which characters are
#: special, and everything outside this set is quoted rather than reasoned about.
#: The backslash is deliberately absent -- to a POSIX shell a bare ``C:\Scans`` is
#: ``C:Scans`` and a bare ``C:\Scans\`` swallows the next token, so every Windows
#: path is quoted. Measured in all three shells; see :func:`_quote_token`.
_BARE_TOKEN_EXTRAS = "_@+=:,./-"

#: Characters no single rendering carries safely into all three shells at once,
#: measured rather than assumed: ``$`` and a backtick expand inside POSIX and
#: PowerShell double quotes, ``%`` expands inside cmd's, ``!`` is history
#: expansion in an interactive bash (``bash: !ns: event not found``), ``"`` ends
#: the quoting, and a newline is a second command. A token holding one of these
#: withholds the whole hint. That is the point: a suggested command that does
#: something other than what it reads is worse than no suggestion at all, and
#: these are rare enough in a scan folder that silence costs nothing.
_UNPASTEABLE = frozenset('"$`%!\n\r')


def _quote_token(token: str) -> str:
    """Return one argv element ready to be pasted back into a shell.

    Double quotes are the one form that makes a metacharacter literal in cmd.exe,
    PowerShell and a POSIX shell alike, so the rendering does not have to guess
    which shell the reader is in. Their single disagreement is a trailing
    backslash: Windows pairs it with the closing quote and POSIX escapes the
    quote with it, and doubling that run is read correctly by both. (Windows
    PowerShell 5.1 passes the doubled run through as two separators when the
    token has no space in it; Windows collapses repeated separators, so the run
    is unchanged -- measured, same plan and same file count either way.)

    Args:
        token: One argv element, exactly as the user typed it.

    Returns:
        The token bare when nothing in it is special to any shell, quoted
        otherwise.
    """
    if token and all(char.isalnum() or char in _BARE_TOKEN_EXTRAS for char in token):
        return token
    trailing = len(token) - len(token.rstrip("\\"))
    return '"' + token + "\\" * trailing + '"'


def normal_run_command(tokens: list[str]) -> str | None:
    """Return the archival run for *tokens*: the same command, plus ``-rw``.

    The whole argv is carried over rather than the input path alone. A run that
    named a provider, a model or a grouping keeps naming it, so the suggestion
    cannot quietly describe a different run from the one just planned -- which is
    the exact failure a hint like this exists to avoid. Nothing can contradict the
    two added flags either, because the caller withholds the hint for any run that
    already spelled a write flag out.

    The program is named ``photokin`` rather than reflected from ``sys.argv[0]``,
    matching every other example in this module: a caller launching
    ``python -m photokin.cli`` gets the spelling the documentation uses.

    Args:
        tokens: The argv the run parsed, without the program name.

    Returns:
        The command line, or None when some token cannot be rendered safely for
        every shell -- in which case there is no hint at all.
    """
    if any(_UNPASTEABLE & set(token) for token in tokens):
        return None
    # A UNC path (``\\server\share``) is the one shape _quote_token's own
    # doubling rule does not cover: that rule is proven only for a *trailing*
    # backslash, and a *leading* double backslash is different -- inside a
    # POSIX double-quoted string ``\\`` is itself an escape for one literal
    # backslash, so a UNC prefix collapses from two to one on that shell and
    # silently stops being a UNC path. Measured: every other Windows path
    # shape (a bare path, one with a trailing backslash) survives a POSIX
    # shlex round trip unchanged; only this one does not.
    if any(token.startswith("\\\\") for token in tokens):
        return None
    return " ".join(["photokin", *(_quote_token(token) for token in tokens), "-rw"])


# === The plan summary ===

#: Width of the summary's label column, sized to its longest label. ``changeset``
#: and ``--dry-run`` are both exactly this wide; ``note`` fits inside it, so the
#: advisory row needed no change here.
_LABEL_WIDTH = 9

#: Where a value starts: the two-space margin, the label column and the ``" : "``
#: between them. Derived from :data:`_LABEL_WIDTH` rather than written out, so a
#: value that runs onto a second line cannot drift out of alignment with the
#: first if the column is ever resized.
_VALUE_INDENT = " " * (2 + _LABEL_WIDTH + len(" : "))

#: The advisory row's prose, wrapped by hand. ``textwrap`` is deliberately not
#: used: the row's last line is the command the reader copies, and a wrapper
#: wide enough to fold this prose would fold a long path across two lines too,
#: producing a hint that breaks the moment it is pasted. Hand-wrapping the prose
#: and never wrapping the command makes that impossible rather than unlikely.
_NORMAL_RUN_NOTE = (
    "this run only prints results - your photos are not read or\n"
    "changed. For the normal archival run:"
)


@dataclass(frozen=True)
class RunPlan:
    """Everything the plan summary states, resolved and ready to render.

    A container rather than a ten-parameter function: every field is decided in
    a different part of ``main``, and freezing them here lets a test assert the
    plan field by field instead of parsing the block back out of stderr.

    Attributes:
        input_location: The input's absolute path -- the folder itself, or the
            manifest or image file. Resolved rather than echoed back, because
            this block exists to answer "am I about to run against the right
            thing", which a relative token cannot answer; the other two path
            lines are absolute for the same reason. Error messages still quote
            what the user typed, so they name the token that has to be fixed.
        input_kind: ``"folder"``, ``"manifest"`` or ``"single photo"`` -- the
            same word the flag-conflict messages use.
        file_count: How many files the run will send.
        group_count: How many groups they form at this granularity.
        group_by: The granularity itself.
        read: The rendered read set, or a ``none (...)`` clause. Stated on every
            run for the same reason the write line is: hydration used to happen
            unasked in manifest mode, and this is where a caller sees that it
            now takes ``-r``.
        output: The rendered destination clause, or ``"stdout"``.
        changeset: The rendered changeset path, or a ``none (...)`` clause.
        write: The rendered write set, or a ``none (...)`` clause.
        sidecars: The rendered markdown-sidecar clause, or a ``none (...)``
            clause. Stated on every run because a sidecar is a file created
            beside the user's images, and ``write`` speaks only for what
            ExifTool puts inside them.
        provider: The provider's display name.
        model: The concrete model string the adapter will send.
        dry_run: Whether the run stops after printing this.
        suggested_command: The ``-rw`` run to advise, from
            :func:`normal_run_command`, or None to print no advice. Held as the
            command rather than as the finished sentence so the wording around it
            stays in this module and cannot be forged by a caller.
    """

    input_location: str
    input_kind: str
    file_count: int
    group_count: int
    group_by: str
    read: str
    output: str
    changeset: str
    write: str
    provider: str
    model: str
    dry_run: bool
    sidecars: str = "none (--sidecar-md off)"
    suggested_command: str | None = None

    def as_dict(self) -> dict:
        """Return every field as a plain JSON-serializable dict.

        The machine-readable twin of :meth:`render`: a caller that watches
        the results NDJSON rather than stderr (a fire-and-forget subprocess
        launch, which cannot see stderr at all) gets the same plan, in the
        ``run: plan`` envelope record, as one dict rather than a block of
        prose it would otherwise have to parse back apart.

        Returns:
            Every field of this plan, by name.
        """
        return asdict(self)

    def render(self) -> str:
        """Return the whole summary block as one multi-line string.

        Returns:
            Eight rows, plus one under ``--dry-run`` and three more lines when a
            command is being advised, emitted as a single log record so nothing
            can be interleaved into the middle of it.
        """
        rows = [
            (
                "input",
                f"{self.input_location} ({self.input_kind}, {self.file_count} file(s) "
                f"in {self.group_count} group(s), group-by {self.group_by})",
            ),
            # Reading precedes everything it affects, so it is stated first.
            ("read", self.read),
            ("output", self.output),
            ("changeset", self.changeset),
            ("write", self.write),
            # Beside ``write`` because it is one: a sidecar is a file this run
            # creates next to the user's images. ``write`` names only what
            # ExifTool puts inside them, so without this row a --sidecar-md all
            # run could print "write: none" immediately before writing a
            # markdown file for every photo, and --dry-run -- which exists to
            # answer "what is about to be touched" -- could not preview it.
            ("sidecars", self.sidecars),
            ("provider", self.provider),
            ("model", self.model),
        ]
        if self.dry_run:
            rows.append(("--dry-run", "stopping here; no model call, and nothing written."))
        if self.suggested_command:
            # Last, under the rows it is drawing a conclusion from: the reader has
            # just been told the run reads nothing and writes nothing, and this
            # says what to type instead. The command carries its own indent so it
            # stands out from the prose above it as the part to copy.
            rows.append(("note", f"{_NORMAL_RUN_NOTE}\n    {self.suggested_command}"))
        lines = []
        for label, value in rows:
            first, *rest = value.split("\n")
            lines.append(f"  {label.ljust(_LABEL_WIDTH)} : {first}")
            lines.extend(f"{_VALUE_INDENT}{line}" for line in rest)
        return "\n".join(["Plan for this run:", *lines])


# === --rename ===
#
# docs/rename-mode.md section 7 is the spec every message below implements;
# section numbers in a docstring below refer to it. The mode flag is
# ``--rename``; ``--rename-undo``, ``--rename-resume`` and ``--rename-finish``
# are the executor commands that read back what it wrote.


def rename_needs_folder_or_manifest(display: str) -> UsageMessage:
    """``--rename`` was given a single photo; it renames a folder's files.

    Args:
        display: The input token exactly as the user typed it.

    Returns:
        The problem line and the remedy line, in that order.
    """
    return (
        f"`--rename` renames a folder, but `{display}` is a single photo.",
        "point --rename at the folder instead: photokin ./scans/ --rename PREFIX",
    )


def rename_mode_conflict(first: str, second: str) -> UsageMessage:
    """Two flags that each drive the run on their own were both given.

    Args:
        first: The first mode flag, as spelled on the command line.
        second: The second mode flag.

    Returns:
        The problem line and the remedy line, in that order.
    """
    return (
        (
            f"`{first}` and `{second}` each run on their own and stop there; "
            "only one can drive this run."
        ),
        f"drop one; run {first} and {second} as two separate commands",
    )


def rename_with_exiftool_write(value: str) -> UsageMessage:
    """``--exiftool-write`` was given beside ``--rename``, which writes no tags.

    Args:
        value: The value ``--exiftool-write`` carried.

    Returns:
        The problem line and the remedy line, in that order.
    """
    return (
        (
            f"`--rename` writes file names, not tags, so `--exiftool-write {value}` "
            "has nothing to do."
        ),
        "drop --exiftool-write; rename mode has its own write switch, -w",
    )


def rename_with_output_file(value: str) -> UsageMessage:
    """``--output-file`` was given beside ``--rename``, which has its own destination.

    Args:
        value: The path ``--output-file`` carried.

    Returns:
        The problem line and the remedy line, in that order.
    """
    return (
        (
            "`--rename` prints a plan, or writes one with --plan-out, so "
            f"`--output-file {value}` would never be written."
        ),
        f"use --plan-out {value} instead",
    )


def rename_write_bundle_contradiction(flag: str, value: str) -> UsageMessage:
    """``-w`` was given beside a flag that contradicts the rename-mode bundle.

    Args:
        flag: The contradicting flag, ``--changeset``.
        value: The value it carried.

    Returns:
        The problem line and the remedy line, in that order.
    """
    return (
        (
            f"`-w` means --changeset true and apply the plan, but `{flag} {value}` "
            "was also given."
        ),
        "drop one; --changeset true alone records the plan without renaming anything",
    )


def rename_managed_by_refuses_write(app: str, catalog: str | None) -> UsageMessage:
    """``-w`` was given for a manifest a catalog application exported (6.1).

    Args:
        app: The ``managed_by.app`` value.
        catalog: The ``managed_by.catalog`` value, or ``None`` when absent.

    Returns:
        The problem line and the remedy line, in that order.
    """
    where = f" ({catalog})" if catalog else ""
    return (
        (
            f"this manifest was exported by {app}{where}; photokin plans the "
            "rename but does not apply it."
        ),
        f"apply the plan through {app}, or use --plan-out to save it for that step",
    )


def rename_digits_invalid(value: int) -> UsageMessage:
    """``--digits`` was given a value less than 1.

    Args:
        value: The value the flag carried.

    Returns:
        The problem line and the remedy line, in that order.
    """
    return (
        f"`--digits {value}` must be at least 1.",
        "use --digits 3 (the default), or another positive width",
    )


def rename_today_invalid(value: str) -> UsageMessage:
    """``--today`` was not a date in ``YYYY-MM-DD`` form.

    Args:
        value: The value the flag carried.

    Returns:
        The problem line and the remedy line, in that order.
    """
    return (
        f"`--today {value}` is not a date in YYYY-MM-DD form.",
        "use a real calendar date, such as --today 2026-08-29",
    )


def rename_command_needs_folder(flag: str, display: str) -> UsageMessage:
    """A rename executor command was given input that names no folder.

    Args:
        flag: The command flag, ``--rename-undo`` or ``--rename-resume``.
        display: The input token exactly as the user typed it, or the empty
            string when no input was given at all.

    Returns:
        The problem line and the remedy line, in that order.
    """
    if not display:
        return (
            (
                f"`{flag}` needs the folder to look for a journal in, or a "
                "journal path of its own."
            ),
            f"name the folder: photokin ./scans/ {flag}",
        )
    return (
        f"`{flag}` needs a folder, but `{display}` is not one.",
        f"point it at that folder, or pass the journal directly: {flag} JOURNAL_PATH",
    )


def rename_no_journal_found(folder: str, verb: str) -> UsageMessage:
    """No journal in *folder* is in the status *verb* needs (5.4).

    Args:
        folder: The folder that was checked.
        verb: ``"undo"`` or ``"resume"``.

    Returns:
        The problem line and the remedy line, in that order.
    """
    wanted = "an applied" if verb == "undo" else "an in-progress or needs-attention"
    return (
        f"no {wanted} rename journal was found in:\n  {folder}",
        f"check the folder by hand, or pass the journal path directly to --rename-{verb}",
    )


def rename_plan_unreadable(path: str, reason: str) -> UsageMessage:
    """A ``--rename-finish`` plan file could not be read as JSON.

    Args:
        path: The plan path.
        reason: The parse or OS error's explanation.

    Returns:
        The problem line and the remedy line, in that order.
    """
    return (
        f"`{path}` cannot be read: {_one_line(reason)}.",
        "re-run --rename --plan-out to make a fresh plan",
    )


def rename_plan_is_not_a_plan(path: str) -> UsageMessage:
    """A ``--rename-finish`` plan file parsed but is not a plan document.

    Args:
        path: The plan path.

    Returns:
        The problem line and the remedy line, in that order.
    """
    return (
        f"`{path}` is not a rename plan (no `entries` list).",
        "point --rename-finish at a file --plan-out wrote",
    )


def _basename(path: str) -> str:
    """Return the final path segment of *path*.

    A hand-rolled split rather than :func:`os.path.basename`, so this module's
    own import list stays exactly what its docstring promises -- nothing
    beyond the standard library's ``dataclasses``.

    Args:
        path: A path, forward- or back-slash separated.

    Returns:
        The text after the last path separator, or *path* itself when it has
        none.
    """
    return path.replace("\\", "/").rsplit("/", 1)[-1]


#: The sentence 5.6 requires at the end of every preview: photokin can plan a
#: rename for a catalog-tracked folder, but only the guard in 6.1 (``managed_by``)
#: can *know* it is one, so the preview always says this out loud instead.
_CATALOG_SENTENCE = (
    "A folder tracked by a catalog application (Lightroom and the like) must "
    "be renamed through that application, not through photokin directly."
)


def render_rename_preview(plan: dict) -> str:
    """Render a rename plan as the default human-readable preview (6.2, 5.6).

    One line per entry (current name, then its target or why it has none),
    each entry's companions indented beneath it, the same-stem files left
    behind, a summary count, every warning and error the plan carries, and
    the catalog sentence 5.6 requires -- on every preview, not only when
    ``managed_by`` is set, since photokin cannot otherwise tell a catalogued
    folder from an ordinary one.

    Args:
        plan: The plan dict, shaped per ``docs/rename-mode.md`` section 6.2.

    Returns:
        The whole preview block, ready to log.
    """
    entries = plan.get("entries") or []
    left_behind = plan.get("left_behind") or []
    warnings = plan.get("warnings") or []
    errors = plan.get("errors") or []

    lines = [f"Rename plan for {plan.get('folder')}:"]
    lines.append(f"  prefix template : {plan.get('prefix_template')}")
    lines.append(f"  digits          : {plan.get('digits')}")
    lines.append(f"  order           : {plan.get('order')}")
    managed_by = plan.get("managed_by")
    if isinstance(managed_by, dict) and managed_by.get("app"):
        lines.append(f"  managed by      : {managed_by['app']}")
    lines.append("")

    renamed = 0
    for entry in entries:
        current = _basename(str(entry.get("path") or ""))
        target = entry.get("target")
        note = f"  ({', '.join(entry['notes'])})" if entry.get("notes") else ""
        if target is None:
            lines.append(f"  {current}  ->  (not planned; see errors below)")
        elif entry.get("changed"):
            renamed += 1
            lines.append(f"  {current}  ->  {target}{note}")
        else:
            lines.append(f"  {current}  (unchanged){note}")
        for companion in entry.get("companions") or []:
            comp_current = _basename(str(companion.get("path") or ""))
            comp_target = companion.get("target")
            if comp_current != comp_target:
                lines.append(f"    + {comp_current}  ->  {comp_target}")

    for item in left_behind:
        lines.append(
            f"  left behind: {_basename(str(item.get('path') or ''))} "
            f"({item.get('reason')})"
        )

    lines.append("")
    lines.append(
        f"  {renamed} file(s) renamed, {len(entries) - renamed} unchanged, "
        f"{len(left_behind)} left behind"
    )

    if warnings:
        lines.append("")
        lines.append("Warnings:")
        lines.extend(f"  - {warning}" for warning in warnings)

    if errors:
        lines.append("")
        lines.append("Errors:")
        lines.extend(f"  - {error}" for error in errors)

    lines.append("")
    lines.append(_CATALOG_SENTENCE)
    return "\n".join(lines)


def render_rename_run_report(
    verb: str,
    status: str,
    *,
    journal_path: str | None,
    renamed: int,
    companions: int,
    unchanged: int,
    left_behind: int,
    skipped: tuple[str, ...],
    stranded: tuple[str, ...],
    warnings: tuple[str, ...],
) -> str:
    """Render the outcome of an apply/undo/resume/finish run (5.3, 5.4, 5.5).

    Args:
        verb: What was asked for -- ``"apply"``, ``"undo"``, ``"resume"`` or
            ``"finish"`` -- named in the header line.
        status: One of ``rename_apply``'s ``STATUS_*`` constants.
        journal_path: The journal written or appended to, or ``None`` when the
            run had nothing to do.
        renamed: Image files renamed by this run.
        companions: Companion files renamed by this run.
        unchanged: Planned files that already had their target name.
        left_behind: Same-stem files the planner declined to carry along.
        skipped: Lines naming what this run declined to do.
        stranded: Paths a failed rollback left out of place, temporaries first.
        warnings: Non-fatal problems, principally sidecar rewrites that did
            not apply.

    Returns:
        The whole report block, ready to log.
    """
    lines = [f"Rename {verb}: {status}"]
    if journal_path:
        lines.append(f"  journal    : {journal_path}")
    lines.append(f"  renamed    : {renamed}")
    lines.append(f"  companions : {companions}")
    lines.append(f"  unchanged  : {unchanged}")
    lines.append(f"  left behind: {left_behind}")
    lines.extend(f"  warning: {warning}" for warning in warnings)
    lines.extend(f"  skipped: {line}" for line in skipped)
    if stranded:
        lines.append("  needs attention -- these files were left mid-move:")
        lines.extend(f"    {path}" for path in stranded)
    return "\n".join(lines)
