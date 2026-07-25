from photokin.exiftool.manifest import DEFAULT_EXIFTOOL_FIELDS, exiftool_records_to_manifest_items


def test_maps_exif_user_comment_into_user_comment() -> None:
    records = [
        {
            "SourceFile": "/tmp/photo.jpg",
            "EXIF:DateTimeOriginal": "1948:05:01 10:23:00",
            "EXIF:UserComment": "Family notes",
            "XMP:Description": "At the square",
        }
    ]

    items = exiftool_records_to_manifest_items(records, DEFAULT_EXIFTOOL_FIELDS)

    assert len(items) == 1
    meta = items[0]["metadata"]
    assert meta["dateTimeOriginal"] == "1948:05:01 10:23:00"
    assert meta["userComment"] == "Family notes"
    assert meta["caption"] == "At the square"


def test_default_fields_include_exif_user_comment() -> None:
    assert "EXIF:UserComment" in DEFAULT_EXIFTOOL_FIELDS


def test_maps_instruction_aliases_into_user_comment() -> None:
    records = [
        {
            "SourceFile": "/tmp/photo.jpg",
            "XMP:Instructions": "Album note",
        }
    ]

    items = exiftool_records_to_manifest_items(records, ["XMP:Instructions"])

    assert len(items) == 1
    assert items[0]["metadata"]["userComment"] == "Album note"


def test_maps_g1_grouped_tag_names() -> None:
    # run_exiftool_json requests -G1, so ExifTool reports fine-grained group names
    # (ExifIFD:/XMP-dc:) rather than the family-0 EXIF:/XMP: names the caller asked
    # for. The requested tags must still be retained and mapped.
    records = [
        {
            "SourceFile": "/tmp/photo.jpg",
            "ExifIFD:DateTimeOriginal": "1948:05:01 10:23:00",
            "ExifIFD:UserComment": "Family notes",
            "XMP-dc:Description": "At the square",
        }
    ]

    items = exiftool_records_to_manifest_items(records, DEFAULT_EXIFTOOL_FIELDS)

    assert len(items) == 1
    meta = items[0]["metadata"]
    assert meta["dateTimeOriginal"] == "1948:05:01 10:23:00"
    assert meta["userComment"] == "Family notes"
    assert meta["caption"] == "At the square"
    assert meta["exiftool"]["ExifIFD:UserComment"] == "Family notes"
