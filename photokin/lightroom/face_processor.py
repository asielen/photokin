#!/usr/bin/env python3
"""Utility helpers for enriching metadata based on Lightroom face tags."""

from __future__ import annotations

from typing import Any, Mapping, MutableMapping, Sequence, TypedDict, cast

from face_utils import faces_to_llm_block

__all__ = [
    "format_faces_in_rows",
    "remove_face_block_from_caption",
    "add_faces_to_caption_rows",
    "add_faces_to_caption",
    "get_face_keywords",
    "get_face_count_info",
    "create_face_summary",
    "build_authoritative_face_tags_section",
    "process_face_tags",
    "get_basic_config",
    "get_row_format_config",
    "get_keyword_config",
    "get_comprehensive_config",
]


class Face(TypedDict, total=False):
    name: str
    centerX: float
    centerY: float
    x: float
    y: float
    w: float
    h: float


class FaceTags(TypedDict, total=False):
    hasFaces: bool
    faces: list[Face]
    formattedString: str
    count: int


PhotoMetadata = MutableMapping[str, Any]
Options = Mapping[str, Any] | None


class _SafeDict(dict):
    def __missing__(self, key: str) -> str:
        return ''

DEFAULT_ROW_OPTIONS: Mapping[str, Any] = {
    'row_threshold': 0.15,
    'separator': ', ',
    'header': 'People Left to Right, Top to Bottom:',
}

DEFAULT_CAPTION_OPTIONS: Mapping[str, Any] = {
    'add_to_caption': True,
    'caption_position': 'after',
    'separator': '\n',
    'skip_if_no_faces': True,
}


def _merge_options(options: Options, defaults: Mapping[str, Any]) -> dict[str, Any]:
    merged = dict(defaults)
    if options:
        merged.update({k: v for k, v in options.items() if v is not None})
    return merged


def _face_coord(face: Mapping[str, Any], primary: str, fallback: str) -> float:
    value = face.get(primary)
    if value is None:
        value = face.get(fallback, 0.0)
    return float(value)


def _sorted_faces(faces: Sequence[Face]) -> list[Face]:
    return sorted(
        faces,
        key=lambda face: (
            _face_coord(face, 'centerY', 'y'),
            _face_coord(face, 'centerX', 'x'),
        ),
    )


def _face_tags(photo_metadata: Mapping[str, Any]) -> FaceTags:
    return cast(FaceTags, photo_metadata.get('faceTags') or {})


def build_authoritative_face_tags_section(photo_metadata: Mapping[str, Any]) -> str:
    """Build the authoritative face-tag block for LLM prompts.

    Args:
        photo_metadata: Photo metadata including a ``faceTags.faces`` list.

    Returns:
        A deterministic ``[FACE TAGS — AUTHORITATIVE]`` section when named
        faces are available, otherwise an empty string.
    """
    face_tags = _face_tags(photo_metadata)
    faces = face_tags.get('faces', [])
    normalized_for_llm = []
    for face in faces:
        normalized_for_llm.append(
            {
                'name': face.get('name'),
                'center_x': face.get('centerX', face.get('x', 0.0)),
                'center_y': face.get('centerY', face.get('y', 0.0)),
                'width': face.get('w', face.get('width', 0.0)),
                'height': face.get('h', face.get('height', 0.0)),
            }
        )
    block = faces_to_llm_block(normalized_for_llm)
    if not block:
        return ''
    return '[FACE TAGS — AUTHORITATIVE]\n' + block


def format_faces_in_rows(photo_metadata: Mapping[str, Any], options: Options = None) -> str:
    """
    Format face names in rows (left-to-right, top-to-bottom).
    
    Args:
        photo_metadata (dict): Photo metadata including faceTags
        options (dict): Formatting options
            - row_threshold (float): Vertical threshold for same row (default: 0.15)
            - separator (str): Separator within rows (default: ', ')
            - header (str): Header text (default: 'People Left to Right, Top to Bottom:')
    
    Returns:
        str: Formatted face block with header and rows
    """
    merged = _merge_options(options, DEFAULT_ROW_OPTIONS)

    face_tags = _face_tags(photo_metadata)

    if not face_tags.get('hasFaces', False):
        return ''

    faces = _sorted_faces(face_tags.get('faces', []))
    if not faces:
        return ''
    
    # Group faces by row
    rows = []
    current_row = []
    last_y = None
    
    for face in faces:
        center_y = _face_coord(face, 'centerY', 'y')
        name = (face.get('name') or 'Unknown').strip() or 'Unknown'

        if last_y is None or abs(center_y - last_y) < merged['row_threshold']:
            # Same row
            current_row.append(name)
            last_y = center_y
        else:
            # New row
            if current_row:
                rows.append(current_row)
            current_row = [name]
            last_y = center_y
    
    # Add last row
    if current_row:
        rows.append(current_row)
    
    # Format rows
    formatted_rows = [merged['separator'].join(row) for row in rows]
    face_block = '\n'.join(formatted_rows)

    return f"{merged['header']}\n{face_block}"


def remove_face_block_from_caption(caption: str, header: str = 'People Left to Right, Top to Bottom:') -> str:
    """
    Remove existing face block from caption.
    
    Args:
        caption (str): Current caption
        header (str): Face block header to look for
    
    Returns:
        str: Caption with face block removed
    """
    if not caption:
        return ''

    start_pos = caption.find(header)
    if start_pos == -1:
        return caption.strip()

    search_start = start_pos + len(header)
    double_newline_pos = caption.find('\n\n', search_start)
    end_pos = double_newline_pos if double_newline_pos != -1 else len(caption)

    before_block = caption[:start_pos].rstrip()
    after_block = caption[end_pos + 2 :] if double_newline_pos != -1 else ''

    cleaned_parts = [part for part in (before_block, after_block.strip()) if part]
    return '\n\n'.join(cleaned_parts)


def add_faces_to_caption_rows(photo_metadata: Mapping[str, Any], options: Options = None) -> str:
    """
    Add face names to caption in row-based format.
    Removes existing face block if present.
    
    Args:
        photo_metadata (dict): Photo metadata including faceTags
        options (dict): Processing options
            - row_threshold (float): Vertical threshold for same row
            - separator (str): Separator within rows
            - header (str): Header text
            - position (str): 'before' or 'after' existing caption
    
    Returns:
        str: Updated caption with face block
    """
    merged = _merge_options(options, {
        'position': 'after',
        'header': 'People Left to Right, Top to Bottom:',
    })
    
    # Get current caption
    current_caption = photo_metadata.get('caption', '') or ''
    
    # Remove existing face block
    current_caption = remove_face_block_from_caption(current_caption, merged['header'])
    
    # Format face block
    face_block = format_faces_in_rows(photo_metadata, options)
    
    if not face_block:
        return current_caption
    
    # Add face block to caption
    if merged['position'] == 'before':
        if current_caption:
            return f"{face_block}\n\n{current_caption}"
        else:
            return face_block
    else:  # after
        if current_caption:
            return f"{current_caption}\n\n{face_block}"
        else:
            return face_block


def add_faces_to_caption(photo_metadata: Mapping[str, Any], options: Options = None) -> str:
    """
    Add face names to photo caption.
    
    Args:
        photo_metadata (dict): Photo metadata including faceTags
        options (dict): Processing options
            - add_to_caption (bool): Whether to add faces to caption (default: True)
            - caption_position (str): 'before' or 'after' existing caption (default: 'after')
            - separator (str): Text between caption and faces (default: '\n')
            - skip_if_no_faces (bool): Don't modify if no faces (default: True)
    
    Returns:
        str: Updated caption with face names
    """
    merged = _merge_options(options, DEFAULT_CAPTION_OPTIONS)

    if not merged['add_to_caption']:
        return photo_metadata.get('caption', '') or ''

    face_tags = _face_tags(photo_metadata)

    if not face_tags.get('hasFaces', False):
        return photo_metadata.get('caption', '') or ''

    face_string = face_tags.get('formattedString', '')
    if not face_string:
        return photo_metadata.get('caption', '') or ''

    existing_caption = photo_metadata.get('caption', '') or ''
    separator = merged['separator']

    if merged['caption_position'] == 'before':
        return separator.join(filter(None, (face_string, existing_caption))) or face_string

    return separator.join(filter(None, (existing_caption, face_string))) or face_string


def get_face_keywords(photo_metadata: Mapping[str, Any], options: Options = None) -> list[str]:
    """
    Extract face names as keywords.
    
    Args:
        photo_metadata (dict): Photo metadata including faceTags
        options (dict): Processing options
            - merge_with_existing (bool): Merge with existing keywords (default: True)
            - deduplicate (bool): Remove duplicate keywords (default: True)
    
    Returns:
        list: List of keywords including face names
    """
    merged = _merge_options(options, {
        'merge_with_existing': True,
        'deduplicate': True,
    })

    face_tags = _face_tags(photo_metadata)
    keywords: list[str] = []

    if merged['merge_with_existing']:
        keywords.extend(photo_metadata.get('keywords', []))

    if face_tags.get('hasFaces', False):
        for face in face_tags.get('faces', []):
            name = face.get('name')
            if isinstance(name, str) and name and name != 'Unknown':
                keywords.append(name)

    if merged['deduplicate']:
        keywords = list(dict.fromkeys(keywords))

    return keywords


def get_face_count_info(photo_metadata: Mapping[str, Any]) -> dict[str, Any]:
    """
    Get information about face count in photo.
    
    Args:
        photo_metadata (dict): Photo metadata including faceTags
    
    Returns:
        dict: Face count information
    """
    face_tags = _face_tags(photo_metadata)
    
    return {
        'has_faces': face_tags.get('hasFaces', False),
        'face_count': face_tags.get('count', 0),
        'face_names': [f.get('name') for f in face_tags.get('faces', [])],
        'formatted_string': face_tags.get('formattedString', ''),
    }


def create_face_summary(photo_metadata: Mapping[str, Any], template: str | None = None) -> str:
    """
    Create a summary text about faces in the photo.
    
    Args:
        photo_metadata (dict): Photo metadata including faceTags
        template (str): Template string with placeholders:
            {count} - number of faces
            {names} - formatted face names
            {filename} - photo filename
    
    Returns:
        str: Formatted summary text
    """
    if template is None:
        template = "Photo of {names}"
    
    face_tags = _face_tags(photo_metadata)
    
    if not face_tags.get('hasFaces', False):
        return ""
    
    placeholders = _SafeDict({
        'count': str(face_tags.get('count', 0)),
        'names': face_tags.get('formattedString', ''),
        'filename': photo_metadata.get('filename', 'unknown'),
    })

    return template.format_map(placeholders)


def process_face_tags(photo_metadata: PhotoMetadata, config: Options = None) -> dict[str, Any]:
    """
    Main processing function for face tags.
    Orchestrates various face tag processing operations.
    
    Args:
        photo_metadata (dict): Photo metadata including faceTags
        config (dict): Configuration options:
            - add_faces_to_caption (bool): Add faces to caption (default: True)
            - use_row_format (bool): Use row-based format (default: False)
            - caption_position (str): 'before' or 'after' (default: 'after')
            - caption_separator (str): Separator text (default: '\n')
            - row_threshold (float): Threshold for same row (default: 0.15)
            - row_separator (str): Separator within rows (default: ', ')
            - row_header (str): Header for row format (default: 'People Left to Right...')
            - add_faces_to_keywords (bool): Add face names as keywords (default: False)
            - create_summary (bool): Create face summary (default: False)
            - summary_template (str): Template for summary
    
    Returns:
        dict: Updated metadata fields
    """
    if config is None:
        config = {}
    
    result: dict[str, Any] = {
        'index': photo_metadata.get('index'),
    }

    def ensure_custom_fields() -> dict[str, Any]:
        custom = result.get('customFields')
        if not isinstance(custom, dict):
            custom = {}
            result['customFields'] = custom
        return cast(dict[str, Any], custom)
    
    # Add faces to caption
    if config.get('add_faces_to_caption', True):
        if config.get('use_row_format', False):
            # Use row-based format
            row_options = {
                'row_threshold': config.get('row_threshold', 0.15),
                'separator': config.get('row_separator', ', '),
                'header': config.get('row_header', 'People Left to Right, Top to Bottom:'),
                'position': config.get('caption_position', 'after'),
            }
            result['caption'] = add_faces_to_caption_rows(photo_metadata, row_options)
        else:
            # Use simple format
            caption_options = {
                'add_to_caption': True,
                'caption_position': config.get('caption_position', 'after'),
                'separator': config.get('caption_separator', '\n'),
                'skip_if_no_faces': True,
            }
            result['caption'] = add_faces_to_caption(photo_metadata, caption_options)
    
    # Add faces to keywords
    if config.get('add_faces_to_keywords', False):
        keyword_options = {
            'merge_with_existing': True,
            'deduplicate': True,
        }
        result['keywords'] = get_face_keywords(photo_metadata, keyword_options)
    
    # Create summary in custom field
    if config.get('create_summary', False):
        summary_template = config.get('summary_template', "Photo of {names}")
        summary = create_face_summary(photo_metadata, summary_template)

        if summary:
            ensure_custom_fields()['faceSummary'] = summary
    
    # Add face count info to custom fields
    face_info = get_face_count_info(photo_metadata)
    if face_info['has_faces']:
        custom_fields = ensure_custom_fields()
        custom_fields['faceCount'] = face_info['face_count']
        custom_fields['faceNames'] = ', '.join(
            name for name in face_info['face_names'] if isinstance(name, str)
        )

    llm_face_section = build_authoritative_face_tags_section(photo_metadata)
    if llm_face_section:
        result['llmFaceTagsAuthoritative'] = llm_face_section

    return result


# Example configurations for different use cases

def get_basic_config():
    """Add face names to caption, nothing else"""
    return {
        'add_faces_to_caption': True,
        'caption_position': 'after',
        'caption_separator': '\n',
        'add_faces_to_keywords': False,
        'create_summary': False,
    }


def get_row_format_config():
    """Add face names to caption in row-based format"""
    return {
        'add_faces_to_caption': True,
        'use_row_format': True,
        'caption_position': 'after',
        'row_threshold': 0.15,
        'row_separator': ', ',
        'row_header': 'People Left to Right, Top to Bottom:',
        'add_faces_to_keywords': False,
        'create_summary': False,
    }


def get_keyword_config():
    """Add face names as keywords"""
    return {
        'add_faces_to_caption': False,
        'add_faces_to_keywords': True,
        'create_summary': False,
    }


def get_comprehensive_config():
    """Add faces to caption and keywords, plus summary"""
    return {
        'add_faces_to_caption': True,
        'caption_position': 'after',
        'caption_separator': '\n\n',
        'add_faces_to_keywords': True,
        'create_summary': True,
        'summary_template': 'Photo featuring {names} ({count} people)',
    }
