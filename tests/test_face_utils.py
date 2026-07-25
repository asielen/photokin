from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from photokin.face_utils import faces_to_llm_block, normalize_faces


def test_normalize_faces_flattens_geometry() -> None:
    payload = {
        "faces": [
            {
                "name": "Bob Church",
                "center": {"x": 0.42, "y": 0.51},
                "width": 0.12,
                "height": 0.14,
            }
        ],
        "count": 1,
    }

    normalized = normalize_faces(payload)

    assert normalized == [
        {
            "name": "Bob Church",
            "center_x": 0.42,
            "center_y": 0.51,
            "width": 0.12,
            "height": 0.14,
        }
    ]


def test_faces_to_llm_block_is_deterministic_and_name_only() -> None:
    faces = [
        {"name": "Bob Church", "center_x": 0.42, "center_y": 0.51, "width": 0.12, "height": 0.14},
        {"name": "", "center_x": 0.11, "center_y": 0.22, "width": 0.10, "height": 0.11},
        {"name": "Jill Smith", "center_x": 0.12, "center_y": 0.31, "width": 0.10, "height": 0.11},
    ]

    block = faces_to_llm_block(faces)

    assert block == "1. Jill Smith (cx=0.120, cy=0.310, w=0.100, h=0.110)\n2. Bob Church (cx=0.420, cy=0.510, w=0.120, h=0.140)"


def test_empty_inputs_are_safe() -> None:
    assert normalize_faces(None) == []
    assert normalize_faces({"faces": [], "count": 0}) == []
    assert faces_to_llm_block([]) == ""
