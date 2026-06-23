#!/usr/bin/env python
"""Extract Lightroom face regions (MWG Regions) from a photo's XMP metadata.

Lightroom stores named face rectangles as MWG Regions, either in a ``.xmp``
sidecar or embedded in the file. This module reads whichever is available and
emits a normalized JSON payload of face names + centers/sizes. Invoked per photo
by the plugin as ``python -m photokin.lightroom.faces_xmp``.

Code map:
- read_sidecar_xmp                  read a classic ``base.xmp`` sidecar (or None)
- read_embedded_xmp                 extract an embedded XMP packet from the file
- _float_attr / _region_name        parse a single region's numbers/name
- parse_face_regions_from_xmp_bytes PUBLIC: XMP bytes -> FaceRegionsPayload
- main                              PUBLIC: CLI entry; prints payload as JSON
"""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path
from typing import Final, TypedDict
import xml.etree.ElementTree as ET

# Namespaces used by Lightroom for MWG Regions
NS: Final[dict[str, str]] = {
    "rdf": "http://www.w3.org/1999/02/22-rdf-syntax-ns#",
    "mwg-rs": "http://www.metadataworkinggroup.com/schemas/regions/",
    "exif": "http://ns.adobe.com/exif/1.0/",
    "x": "adobe:ns:meta/",
    "xmp": "http://ns.adobe.com/xap/1.0/",
    "lr": "http://ns.adobe.com/lightroom/1.0/",
    "iptcExt": "http://iptc.org/std/Iptc4xmpExt/2008-02-29/",
}


STAREA_URI: Final[str] = "http://ns.adobe.com/xmp/sType/Area#"
MWG_RS_URI: Final[str] = NS["mwg-rs"]


class FaceRegion(TypedDict, total=False):
    """One named face rectangle: name plus normalized center and size (0..1)."""

    name: str | None
    center: dict[str, float]
    width: float
    height: float


class FaceRegionsPayload(TypedDict):
    """The emitted result: the list of faces and their count."""

    faces: list[FaceRegion]
    count: int


__all__ = [
    "read_sidecar_xmp",
    "read_embedded_xmp",
    "parse_face_regions_from_xmp_bytes",
    "main",
]


def read_sidecar_xmp(path: Path) -> bytes | None:
    """
    Try to read a classic Lightroom XMP sidecar.

    Lightroom's pattern is "base.xmp" (same basename, .xmp extension),
    e.g. "photo.tif" -> "photo.xmp".
    """
    sidecar = path.with_suffix(".xmp")
    if sidecar.is_file():
        try:
            return sidecar.read_bytes()
        except OSError:
            return None
    return None


def read_embedded_xmp(path: Path) -> bytes | None:
    """
    Try to extract an embedded XMP packet from the image file itself.

    We look for the <x:xmpmeta ...> ... </x:xmpmeta> block and return
    those bytes. This works for most JPEG/TIFF/PSD/DNG written by Adobe.
    """
    try:
        data = path.read_bytes()
    except OSError:
        return None

    start_marker = b"<x:xmpmeta"
    end_marker = b"</x:xmpmeta>"

    start = data.find(start_marker)
    if start == -1:
        return None

    end = data.find(end_marker, start)
    if end == -1:
        return None

    end += len(end_marker)
    return data[start:end]


def _float_attr(elem: ET.Element, qname: str, default: float = 0.0) -> float:
    v = elem.get(qname)
    if not v:
        return default
    try:
        return float(v)
    except ValueError:
        return default


def _region_name(desc: ET.Element) -> str | None:
    namespaced_candidates = (
        f"{{{MWG_RS_URI}}}Name",
        f"{{{NS['xmp']}}}RegionName",
        f"{{{NS['lr']}}}PersonDisplayName",
        f"{{{NS['lr']}}}PersonInImage",
    )
    for qname in namespaced_candidates:
        value = desc.get(qname)
        if value and value.strip():
            return value.strip()

    text_candidates = (
        "mwg-rs:Name",
        "xmp:RegionName",
        "lr:PersonDisplayName",
        "lr:PersonInImage",
    )
    for xpath in text_candidates:
        value = desc.findtext(xpath, default="", namespaces=NS)
        if value and value.strip():
            return value.strip()

    return None


def parse_face_regions_from_xmp_bytes(blob: bytes) -> FaceRegionsPayload:
    """
    Parse MWG Regions Face data from an XMP packet (as bytes).

    Lightroom typically writes something like:

        <mwg-rs:RegionList>
          <rdf:Bag>
            <rdf:li>
              <rdf:Description
                  mwg-rs:Type="Face"
                  mwg-rs:Name="Alice"
                  mwg-rs:Rotation="0.00000">
                <mwg-rs:Area
                   stArea:h="..."
                   stArea:w="..."
                   stArea:x="..."
                   stArea:y="..."/>
              </rdf:Description>
            </rdf:li>
          </rdf:Bag>
        </mwg-rs:RegionList>
    """
    try:
        tree = ET.parse(io.BytesIO(blob))
    except ET.ParseError:
        return {"faces": [], "count": 0}

    root = tree.getroot()
    faces: list[FaceRegion] = []

    # Walk all RegionList elements anywhere in the packet
    for region_list in root.findall(".//mwg-rs:RegionList", NS):
        bag = region_list.find("rdf:Bag", NS)
        if bag is None:
            continue

        for li in bag.findall("rdf:li", NS):
            desc = li.find("rdf:Description", NS)
            if desc is None:
                continue

            # Type / Name / Rotation typically live as ATTRIBUTES
            r_type = (
                desc.get(f"{{{MWG_RS_URI}}}Type")
                or desc.findtext("mwg-rs:Type", default="", namespaces=NS)
                or ""
            )
            if r_type != "Face":
                continue

            name = _region_name(desc)

            area = desc.find("mwg-rs:Area", NS)
            if area is None:
                continue

            x = _float_attr(area, f"{{{STAREA_URI}}}x", 0.0)
            y = _float_attr(area, f"{{{STAREA_URI}}}y", 0.0)
            width = _float_attr(area, f"{{{STAREA_URI}}}w", 0.0)
            height = _float_attr(area, f"{{{STAREA_URI}}}h", 0.0)

            faces.append(
                {
                    "name": name,
                    "center": {"x": x, "y": y},
                    "width": width,
                    "height": height,
                }
            )

    return {"faces": faces, "count": len(faces)}


def main(argv: list[str]) -> int:
    """Entry point used by both the CLI wrapper and Lightroom subprocesses."""
    if len(argv) != 2:
        sys.stderr.write("Usage: python -m photokin.lightroom.faces_xmp /path/to/photo.ext\n")
        return 1


    img_path = Path(argv[1])
    result: dict[str, object] = {"path": str(img_path), "faces": [], "count": 0}



    if not img_path.is_file():
        print(json.dumps(result, ensure_ascii=False))
        return 0

    # 1) Try sidecar XMP
    blob = read_sidecar_xmp(img_path)

    # 2) Fall back to embedded XMP inside the image file
    if blob is None:
        blob = read_embedded_xmp(img_path)

    if blob is None:
        print(json.dumps(result, ensure_ascii=False))
        return 0

    # # --- DEBUG: write raw XMP for inspection ---
    # try:
    #     debug_path = img_path.with_suffix(img_path.suffix + ".raw_xmp.txt")
    #     debug_path.write_bytes(blob)
    # except Exception:
    #     # Never fail the main workflow because of debug logging
    #     pass

    faces_payload = parse_face_regions_from_xmp_bytes(blob)
    result["faces"] = faces_payload["faces"]
    result["count"] = faces_payload["count"]
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
