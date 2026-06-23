from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from photokin.lightroom.face_utils import faces_to_llm_block, normalize_faces
from photokin.lightroom.faces_xmp import parse_face_regions_from_xmp_bytes


XMP_SAMPLE = b"""<?xpacket begin='' id='W5M0MpCehiHzreSzNTczkc9d'?>
<x:xmpmeta xmlns:x='adobe:ns:meta/' xmlns:rdf='http://www.w3.org/1999/02/22-rdf-syntax-ns#'>
  <rdf:RDF>
    <rdf:Description
      xmlns:mwg-rs='http://www.metadataworkinggroup.com/schemas/regions/'
      xmlns:stArea='http://ns.adobe.com/xmp/sType/Area#'
      xmlns:xmp='http://ns.adobe.com/xap/1.0/'>
      <mwg-rs:Regions>
        <rdf:Bag>
          <rdf:li>
            <rdf:Description mwg-rs:Type='Face' xmp:RegionName='Bob Church'>
              <mwg-rs:Area stArea:x='0.42' stArea:y='0.51' stArea:w='0.12' stArea:h='0.14'/>
            </rdf:Description>
          </rdf:li>
          <rdf:li>
            <rdf:Description mwg-rs:Type='Face'>
              <mwg-rs:Name>Jill Smith</mwg-rs:Name>
              <mwg-rs:Area stArea:x='0.12' stArea:y='0.31' stArea:w='0.10' stArea:h='0.11'/>
            </rdf:Description>
          </rdf:li>
        </rdf:Bag>
      </mwg-rs:Regions>
      <mwg-rs:RegionList>
        <rdf:Bag>
          <rdf:li>
            <rdf:Description mwg-rs:Type='Face'>
              <mwg-rs:Area stArea:x='0.20' stArea:y='0.25' stArea:w='0.08' stArea:h='0.09'/>
            </rdf:Description>
          </rdf:li>
        </rdf:Bag>
      </mwg-rs:RegionList>
    </rdf:Description>
  </rdf:RDF>
</x:xmpmeta>
"""


def test_extracts_faces_in_required_structure() -> None:
    payload = parse_face_regions_from_xmp_bytes(XMP_SAMPLE)

    assert payload["count"] == 1
    assert payload["faces"] == [
        {
            "name": None,
            "center": {"x": 0.20, "y": 0.25},
            "width": 0.08,
            "height": 0.09,
        }
    ]


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
