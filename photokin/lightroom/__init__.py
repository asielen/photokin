"""Lightroom-facing helpers invoked by the MEL plugin as subprocess modules.

Each module here is runnable as ``python -m photokin.lightroom.<name>`` and forms
part of the plugin<->library runtime contract (see the repo's
LIGHTROOM_INTEGRATION.md):

- faces_xmp          read Lightroom face regions from a photo's XMP
- face_utils         normalize/format face data for prompts and captions
- face_processor     higher-level face-tag processing built on face_utils
- exiftool_manifest  build a manifest from ExifTool JSON output
- caption_border     render a Polaroid-style caption border onto an export
- face_tag_examples  copy-paste recipes (not invoked by the plugin)
"""
