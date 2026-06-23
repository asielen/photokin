"""Stable, forward-compatible public API for embedders (Lightroom scripts, CLIs).

These thin wrappers delegate straight to :mod:`photokin.core` but exist as a
deliberate stability seam: callers import from here so the core module layout can
be refactored freely without breaking downstream code. Keep these signatures
conservative and additive — they are the promise the package makes to outside
consumers.
"""

from __future__ import annotations
from typing import Optional, Dict, Any, Callable

from . import core  # import the module, not the function
from .utils import Config


def analyze_photo(
    front_path: str,
    back_path: Optional[str] = None,
    *,
    meta: Optional[Dict[str, Any]] = None,
    cfg: Optional[Config] = None,
    log: Callable[[str], None] = print,
) -> Dict[str, Any]:
    """Forward-compatible wrapper around :func:`photokin.core.analyze_photo`.

    Keeping this shim thin means Lightroom scripts can upgrade independently of
    the core package while still reusing the shared validation and retries.
    """

    cfg = cfg or Config()
    return core.analyze_photo(front_path, back_path, cfg, original_meta=meta)


def analyze_folder(
    folder_path: str,
    *,
    cfg: Optional[Config] = None,
    discover_meta: bool = True,
    # log: Callable[[str], None] = print,
) -> Dict[str, Any]:
    """
    Mirror the historic API while routing to the current core implementation.

    ``discover_meta`` used to gate a slow filesystem walk, but the modern core
    always loads metadata when needed.  We keep the parameter to avoid breaking
    callers even though it is ignored.
    """

    # ``discover_meta`` is preserved for compatibility with earlier callers, but the
    # modern implementation always discovers metadata internally, so we ignore it.
    _ = discover_meta
    cfg = cfg or Config()
    return core.analyze_folder(folder_path, cfg)


def analyze_manifest(
    manifest: Dict[str, Any] | str,
    *,
    cfg: Optional[Config] = None,
    update_policy: str = "merge_per_variant",
    # log: Callable[[str], None] = print,
) -> Dict[str, Any]:
    """
    Expose the manifest helper implemented in :mod:`photokin.core`.

    The wrapper exists so plug-in builds can continue importing from the public
    surface while we freely refactor the core module layout.
    """

    cfg = cfg or Config()
    return core.analyze_manifest(manifest, cfg, update_policy=update_policy)
