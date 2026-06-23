#!/usr/bin/env python3
"""Reusable Lightroom face-tag processing recipes for quick copy/paste.

These are illustrative usage patterns for :mod:`photokin.lightroom.face_processor`,
not part of the plugin's runtime path. Each ``example_*`` function takes photo
metadata + config and returns the processor's result, showing one way to shape
captions/keywords from face data.

Code map:
- _process_if_faces        run the processor only when the photo has faces
- example_basic            append face names to the caption
- example_natural_language natural-language description of who's pictured
- example_keywords_only    add faces as keywords, leave caption untouched
- example_comprehensive    caption + keywords + custom summary fields
- example_before_caption   prepend face names to the existing caption
- example_conditional      only process photos with certain keywords
- example_formal           formal documentation-style captions
- example_event_attendees  document attendees in the caption
- example_custom_field     store face info in a custom field only
- example_portrait_detection auto-categorize by face count
"""

from __future__ import annotations

from typing import Any, Mapping

from photokin.lightroom import face_processor

__all__ = [
    'example_basic',
    'example_natural_language',
    'example_keywords_only',
    'example_comprehensive',
    'example_formal',
    'example_event_attendees',
    'example_custom_field',
    'example_portrait_detection',
]


def _process_if_faces(photo_metadata: Mapping[str, Any], config: Mapping[str, Any]) -> dict[str, Any]:
    """Shared helper that only runs the processor when the file has faces."""

    updated: dict[str, Any] = {'index': photo_metadata.get('index')}
    if photo_metadata.get('faceTags', {}).get('hasFaces'):
        face_result = face_processor.process_face_tags(photo_metadata, config)
        updated.update(face_result)
    return updated

# =============================================================================
# EXAMPLE 1: Basic - Add face names to caption
# =============================================================================
def example_basic(photo_metadata):
    """
    Simply adds face names to the end of the caption.
    
    Input:
        Caption: "Birthday party at the beach"
        Faces: Alice, Bob, Carol
    
    Output:
        Caption: "Birthday party at the beach\nAlice, Bob, Carol"
    """
    face_config = {
        'add_faces_to_caption': True,
        'caption_position': 'after',
        'caption_separator': '\n',
        'add_faces_to_keywords': False,
        'create_summary': False,
    }
    return _process_if_faces(photo_metadata, face_config)


# =============================================================================
# EXAMPLE 2: Natural Language
# =============================================================================
def example_natural_language(photo_metadata):
    """
    Creates natural language descriptions.
    
    Configure in Lightroom:
        - Prefix: "Photo with "
        - Separator: " and "
    
    Input:
        Caption: "Summer vacation"
        Faces: Alice, Bob
    
    Output:
        Caption: "Summer vacation\nPhoto with Alice and Bob"
    """
    face_config = {
        'add_faces_to_caption': True,
        'caption_position': 'after',
        'caption_separator': '\n',
    }
    return _process_if_faces(photo_metadata, face_config)


# =============================================================================
# EXAMPLE 3: Keywords Only
# =============================================================================
def example_keywords_only(photo_metadata):
    """
    Adds face names as keywords without modifying caption.
    
    Input:
        Keywords: ["vacation", "beach"]
        Faces: Alice, Bob, Carol
    
    Output:
        Keywords: ["vacation", "beach", "Alice", "Bob", "Carol"]
        Caption: unchanged
    """
    face_config = {
        'add_faces_to_caption': False,
        'add_faces_to_keywords': True,
        'create_summary': False,
    }
    return _process_if_faces(photo_metadata, face_config)


# =============================================================================
# EXAMPLE 4: Comprehensive
# =============================================================================
def example_comprehensive(photo_metadata):
    """
    Adds faces to caption, keywords, and creates custom summary fields.
    
    Input:
        Caption: "Team meeting"
        Keywords: ["work", "office"]
        Faces: Alice, Bob, Carol
    
    Output:
        Caption: "Team meeting\n\nAlice, Bob, Carol"
        Keywords: ["work", "office", "Alice", "Bob", "Carol"]
        Custom Fields:
            - faceSummary: "Photo featuring Alice, Bob, Carol (3 people)"
            - faceCount: 3
            - faceNames: "Alice, Bob, Carol"
    """
    face_config = {
        'add_faces_to_caption': True,
        'caption_position': 'after',
        'caption_separator': '\n\n',
        'add_faces_to_keywords': True,
        'create_summary': True,
        'summary_template': 'Photo featuring {names} ({count} people)',
    }
    return _process_if_faces(photo_metadata, face_config)


# =============================================================================
# EXAMPLE 5: Before Caption
# =============================================================================
def example_before_caption(photo_metadata):
    """
    Puts face names before the existing caption.
    
    Configure in Lightroom:
        - Prefix: ""
        - Separator: ", "
    
    Input:
        Caption: "Birthday party"
        Faces: Alice, Bob
    
    Output:
        Caption: "Alice, Bob - Birthday party"
    """
    face_config = {
        'add_faces_to_caption': True,
        'caption_position': 'before',
        'caption_separator': ' - ',
    }
    return _process_if_faces(photo_metadata, face_config)


# =============================================================================
# EXAMPLE 6: Conditional Processing
# =============================================================================
def example_conditional(photo_metadata):
    """
    Only processes photos with certain keywords.
    
    Input:
        Keywords: ["family"] → Processes faces
        Keywords: ["landscape"] → Skips faces
    """
    updated = {'index': photo_metadata.get('index')}

    keywords = photo_metadata.get('keywords', [])
    should_process = any(kw.lower() in ['family', 'portrait', 'people'] for kw in keywords)

    if should_process and photo_metadata.get('faceTags', {}).get('hasFaces'):
        face_config = {
            'add_faces_to_caption': True,
            'caption_position': 'after',
            'caption_separator': '\n',
        }
        updated.update(face_processor.process_face_tags(photo_metadata, face_config))

    return updated


# =============================================================================
# EXAMPLE 7: Formal Documentation Style
# =============================================================================
def example_formal(photo_metadata):
    """
    Creates formal documentation-style captions.
    
    Configure in Lightroom:
        - Prefix: "Pictured (L-R): "
        - Separator: ", "
    
    Input:
        Caption: "Q4 2024 Team Meeting"
        Faces: Alice, Bob, Carol
    
    Output:
        Caption: "Q4 2024 Team Meeting\nPictured (L-R): Alice, Bob, Carol"
    """
    face_config = {
        'add_faces_to_caption': True,
        'caption_position': 'after',
        'caption_separator': '\n',
    }
    return _process_if_faces(photo_metadata, face_config)


# =============================================================================
# EXAMPLE 8: Event Attendees
# =============================================================================
def example_event_attendees(photo_metadata):
    """
    Documents event attendees in caption.
    
    Configure in Lightroom:
        - Prefix: "Attended by: "
        - Separator: ", "
    
    Input:
        Caption: "Annual Company Picnic 2024"
        Faces: Alice, Bob, Carol, David
    
    Output:
        Caption: "Annual Company Picnic 2024\nAttended by: Alice, Bob, Carol, David"
    """
    face_config = {
        'add_faces_to_caption': True,
        'caption_position': 'after',
        'caption_separator': '\n',
    }
    return _process_if_faces(photo_metadata, face_config)


# =============================================================================
# EXAMPLE 9: Custom Field Only
# =============================================================================
def example_custom_field(photo_metadata):
    """
    Stores face info in custom field without modifying caption or keywords.
    
    Output:
        Caption: unchanged
        Keywords: unchanged
        Custom Fields:
            - peopleInPhoto: "Alice, Bob, Carol"
            - photoSubjects: 3
    """
    updated = {'index': photo_metadata.get('index')}

    if photo_metadata.get('faceTags', {}).get('hasFaces'):
        face_info = face_processor.get_face_count_info(photo_metadata)

        updated['customFields'] = {
            'peopleInPhoto': face_info['formatted_string'],
            'photoSubjects': face_info['face_count'],
        }

    return updated


# =============================================================================
# EXAMPLE 10: Portrait Detection
# =============================================================================
def example_portrait_detection(photo_metadata):
    """
    Auto-categorizes photos based on number of faces.
    
    Output:
        1 face: Keyword "portrait"
        2 faces: Keyword "couple"
        3+ faces: Keyword "group"
    """
    updated = {'index': photo_metadata.get('index')}
    keywords = list(photo_metadata.get('keywords', []))
    
    face_info = face_processor.get_face_count_info(photo_metadata)
    
    if face_info['has_faces']:
        count = face_info['face_count']
        
        if count == 1:
            keywords.append('portrait')
        elif count == 2:
            keywords.append('couple')
        elif count >= 3:
            keywords.append('group')
        
        updated['keywords'] = keywords
    
    return updated


# =============================================================================
# How to use these examples:
# =============================================================================
"""
1. Choose an example that fits your needs
2. Copy the function code
3. Paste into metadata_server.py's process_photo_metadata() function
4. Or call the function directly:

    def process_photo_metadata(photo_metadata):
        return example_basic(photo_metadata)

5. Restart the server
6. Process your photos!
"""
