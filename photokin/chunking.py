"""Split an ordered list of analysis parts into contiguous, bounded chunks.

Document mode's Phase 3 problem: a group's payload (the ``(label, [paths])``
parts ``process_manifest_stream`` builds -- for a multipage group, Page 1..N
first, then Front, Back, Negative) can be arbitrarily large. A 63-page
memoir is 63+ images in one model call today. :func:`partition_parts` is the
pure split that turns that into several bounded calls; the calls themselves,
and the consolidation pass that reconciles their results, are W6's concern
(``core.py``), not this module's -- this module has no knowledge of the
model, the provider, or the record shape, and imports nothing beyond the
standard library so it cannot become a cycle back into ``photokin.core``.

Two consequences of the rules below are worth stating rather than leaving for
someone to discover by reading the loop:

- **A front and its back are never split apart.** Every non-page part
  (``Front``, ``Back``, ``Negative``) rides in chunk 1, so a front/back pair
  -- which is exactly two non-page parts -- always lands in the same chunk
  (D3 in ``docs/document-mode.md``).
- **A group with no page parts is never chunked at all**, however many
  variant scans it holds. There is no safe boundary inside a non-page group:
  splitting ``Front``/``Back``/``Negative`` apart would separate views of the
  same physical object, so rule 1 below opts the whole group out.
- **Flattening the returned chunks reproduces every part of the input
  exactly once, but not always in input order.** Chunk 1's budget is reduced
  by the images ``others`` will ride with (rule 4), so whenever a group both
  carries a non-page part and needs more than one page chunk, ``others``
  lands ahead of a later page chunk in the flattened output -- e.g. pages
  1-6, then Front/Back, then pages 7-63, rather than pages 1-63 then
  Front/Back. This is the accepted cost of keeping the first call balanced
  rather than systematically the largest; callers that care about final
  document order read it from part *labels* (page numbers), never from
  chunk position.

See ``docs/document-mode-contract.md`` section 5 for the frozen contract this
implements.
"""

from __future__ import annotations

import re
from typing import Final

#: Matches a page-part label ("Page 1", "page  12"); the vocabulary is frozen
#: by the contract, so this pattern is deliberately narrow rather than
#: guessing at other shapes.
_PAGE_LABEL_RE: Final[re.Pattern[str]] = re.compile(r"^Page\s+\d+$", re.IGNORECASE)


def partition_parts(
    parts: list[tuple[str, list[str]]],
    chunk_size: int,
) -> list[list[tuple[str, list[str]]]]:
    """Split ordered parts into contiguous blocks of at most ``chunk_size`` images.

    Args:
        parts: The ordered ``(label, [paths])`` parts of one group's payload,
            as built by ``process_manifest_stream`` -- for a multipage group,
            ``Page 1``..``Page N`` first, then ``Front``, ``Back``,
            ``Negative``. Not mutated.
        chunk_size: The per-call image budget. ``<= 0`` disables chunking.

    Returns:
        A list of chunks, each a list of ``(label, [paths])`` parts in the
        same shape as ``parts``. Concatenating every chunk's parts, in
        order, reproduces ``parts`` exactly -- no part is ever dropped,
        duplicated, or reordered relative to its neighbors of the same kind.
        A single part is never split across chunks: a page with more variant
        scans than ``chunk_size`` becomes its own oversized chunk.

    Raises:
        Nothing. Every input, including an empty list or a negative
        ``chunk_size``, has a defined, non-raising result.
    """
    total_images = sum(len(paths) for _, paths in parts)
    pages = [(label, paths) for label, paths in parts if _PAGE_LABEL_RE.match(label)]

    if chunk_size <= 0 or total_images <= chunk_size or not pages:
        # Rule 1: the ordinary case (small groups, chunking disabled, or a
        # group with no page parts to safely split on) is one chunk that is
        # `parts` unchanged -- this is what keeps an ordinary front/back
        # group's call sequence byte-identical to today's (D3), and what
        # makes --max-images-per-call 0 restore single-call behavior at any
        # size.
        return [list(parts)]

    others = [(label, paths) for label, paths in parts if not _PAGE_LABEL_RE.match(label)]
    others_images = sum(len(paths) for _, paths in others)

    # Rule 4: chunk 1's page budget is reduced by the images `others` will
    # ride with, so the first call is not systematically the largest -- but
    # it always gets at least one page part, however large `others` is, so
    # that a page-heavy group with a huge Front/Back pair still makes
    # progress on page 1 in the very first call.
    first_chunk_budget = max(chunk_size - others_images, 1)

    chunks: list[list[tuple[str, list[str]]]] = []
    current: list[tuple[str, list[str]]] = []
    current_count = 0
    budget = first_chunk_budget
    for label, paths in pages:
        part_count = len(paths)
        # Rule 3: a part is atomic -- its variant scans are of one physical
        # page and never straddle a chunk boundary. A part alone bigger than
        # the budget becomes its own oversized chunk rather than being split,
        # so it starts a fresh chunk (flushing whatever was pending) and is
        # immediately flushed itself.
        if current and current_count + part_count > budget:
            chunks.append(current)
            current = []
            current_count = 0
            budget = chunk_size
        current.append((label, paths))
        current_count += part_count

    # `pages` is non-empty here (the early-return above catches that case),
    # so the loop above ran at least once and `current` always holds the
    # trailing chunk being built.
    chunks.append(current)

    # Rule 5: append `others` to chunk 1, after its pages. Pages precede
    # Front/Back/Negative in the source `parts` list, so this makes the
    # concatenation of the returned chunks exactly equal to `parts`.
    chunks[0] = chunks[0] + others

    return chunks
