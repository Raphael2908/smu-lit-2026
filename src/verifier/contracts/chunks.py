"""The pre-contextualisation chunk, and the token estimate it is cut against.

This lives in ``contracts`` rather than in ``semantic`` because it crosses a module
boundary: L0 splits the AI output into claims ONCE and hands the list to L2 and L3 on
``LayerInput.claims``, so the type has to be namable by ``contracts.layers`` without
``contracts`` importing ``semantic`` (which imports ``contracts``, and would be a cycle).

``semantic.chunking`` re-exports both names, so every existing import still works.
"""

from __future__ import annotations

from dataclasses import dataclass

from verifier.contracts.citations import Span
from verifier.contracts.enums import ChunkKind

__all__ = ["CHARS_PER_TOKEN", "RawChunk", "estimate_tokens"]

#: Characters per token. A heuristic, NOT a tokeniser: it is deliberately crude so that
#: chunking never depends on a vendor tokeniser being importable, and deliberately
#: conservative (real English averages ~4.0-4.7 chars/token, legal prose with citations
#: and paragraph markers sits at the low end) so the estimate over-counts rather than
#: under-counts. Over-counting shrinks chunks; under-counting overflows the context
#: window, which is the failure we cannot detect at runtime.
CHARS_PER_TOKEN = 4


def estimate_tokens(text: str) -> int:
    """Approximate token count at ~4 chars/token.

    APPROXIMATE BY DESIGN. Exact counts would need the vendor tokeniser, which would
    make offline chunking depend on a network-installed model. The number is only ever
    used to decide where to cut, and every cut is verified against a hard character
    budget afterwards, so an error here costs chunk evenness, never correctness.
    """
    return (len(text) + CHARS_PER_TOKEN - 1) // CHARS_PER_TOKEN


@dataclass(frozen=True)
class RawChunk:
    """A chunk before contextualisation.

    Deliberately *not* :class:`~verifier.contracts.documents.Chunk`: that contract
    requires ``embed_input`` and its hash, which only exist once the document summary
    and heading path have been prefixed. Keeping the two apart means the cache key is
    always derived from the exact string that was sent to the model, never from the
    bare text. ``heading_path`` lives here and not on ``Chunk`` for the same reason --
    it is an input to the embed string, not a property of the embedded unit.
    """

    ordinal: int
    kind: ChunkKind
    text: str
    heading_path: tuple[str, ...] = ()
    paragraph_from: int | None = None
    paragraph_to: int | None = None
    #: Offsets into the AI output. Present for output chunks (L2 uses them to decide
    #: which claims a citation is responsible for), absent for source chunks.
    span: Span | None = None
    #: Provenance of the split, surfaced in ``LayerResult.detail`` so a reviewer can see
    #: whether claims came from the model or from the deterministic fallback.
    strategy: str = "paragraph"

    @property
    def estimated_tokens(self) -> int:
        return estimate_tokens(self.text)
