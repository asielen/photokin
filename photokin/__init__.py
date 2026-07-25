"""
photokin
========

Public API surface for the photokin library.

Exports:
- Config: runtime configuration dataclass.
- analyze_photo: run the full pipeline for one photo (front + optional back).
- analyze_folder: batch mode for a folder of images.
"""

from .utils import Config
from .core import analyze_photo, analyze_folder

__all__ = ["Config", "analyze_photo", "analyze_folder"]
