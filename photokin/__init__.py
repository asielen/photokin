"""
photo_archiver.__init__
=======================

Public API surface for the photo archiver library.

Exports:
- Config: runtime configuration dataclass.
- analyze_photo: main library function to run the full pipeline.
"""

from .utils import Config
from .core import analyze_photo, analyze_folder

__all__ = ["Config", "analyze_photo", "analyze_folder"]
