#!/usr/bin/env python3
"""Utility helpers for normalizing and formatting extracted face metadata."""

from __future__ import annotations

from typing import Mapping, Sequence, TypedDict


class NormalizedFace(TypedDict):
    """Flattened face metadata used by downstream consumers."""

    name: str | None
    center_x: float
    center_y: float
    width: float
    height: float


def _to_float(value: object, default: float = 0.0) -> float:
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float, str)):
        try:
            return float(value)
        except ValueError:
            return default
    return default


def normalize_faces(face_data: Mapping[str, object] | None) -> list[NormalizedFace]:
    """Normalize extracted face payload into a flattened list.

    Args:
        face_data: Dictionary returned by ``photokin.lightroom.faces_xmp.parse_face_regions_from_xmp_bytes``.

    Returns:
        A list of normalized faces with flattened geometry values.
    """
    if not face_data:
        return []

    faces = face_data.get("faces")
    if not isinstance(faces, Sequence):
        return []

    normalized: list[NormalizedFace] = []
    for face in faces:
        if not isinstance(face, Mapping):
            continue
        center = face.get("center")
        center_map = center if isinstance(center, Mapping) else {}
        name = face.get("name")
        normalized.append(
            {
                "name": str(name).strip() if isinstance(name, str) and name.strip() else None,
                "center_x": _to_float(center_map.get("x")),
                "center_y": _to_float(center_map.get("y")),
                "width": _to_float(face.get("width")),
                "height": _to_float(face.get("height")),
            }
        )

    return normalized


def faces_to_llm_block(faces: Sequence[Mapping[str, object]] | None) -> str:
    """Create a deterministic numbered list from normalized faces.

    Args:
        faces: Sequence of normalized face dictionaries.

    Returns:
        Numbered list of named faces sorted by ``center_x`` then ``center_y``.
        Geometry is emitted as ``(cx=..., cy=..., w=..., h=...)`` rounded to
        three decimals. Returns an empty string when no usable names are present.
    """
    if not faces:
        return ""

    named_faces: list[tuple[str, float, float, float, float]] = []
    for face in faces:
        raw_name = face.get("name") if isinstance(face, Mapping) else None
        if isinstance(raw_name, str):
            stripped_name = raw_name.strip()
            if stripped_name:
                named_faces.append(
                    (
                        stripped_name,
                        _to_float(face.get("center_x")),
                        _to_float(face.get("center_y")),
                        _to_float(face.get("width")),
                        _to_float(face.get("height")),
                    )
                )

    if not named_faces:
        return ""

    named_faces.sort(key=lambda item: (item[1], item[2]))
    lines = [
        f"{idx}. {name} (cx={center_x:.3f}, cy={center_y:.3f}, w={width:.3f}, h={height:.3f})"
        for idx, (name, center_x, center_y, width, height) in enumerate(named_faces, start=1)
    ]
    return "\n".join(lines)


def face_tags_to_llm_block(face_tags: Mapping[str, object] | None) -> str:
    """Build a face list block from manifest ``faceTags`` data."""
    if not face_tags:
        return ""
    faces = face_tags.get("faces") if isinstance(face_tags, Mapping) else None
    if not isinstance(faces, Sequence):
        return ""

    normalized: list[NormalizedFace] = []
    for face in faces:
        if not isinstance(face, Mapping):
            continue
        center = face.get("center")
        center_map = center if isinstance(center, Mapping) else {}
        normalized.append(
            {
                "name": str(face.get("name")).strip() if isinstance(face.get("name"), str) else None,
                "center_x": _to_float(face.get("centerX", center_map.get("x"))),
                "center_y": _to_float(face.get("centerY", center_map.get("y"))),
                "width": _to_float(face.get("w", face.get("width"))),
                "height": _to_float(face.get("h", face.get("height"))),
            }
        )

    return faces_to_llm_block(normalized)
