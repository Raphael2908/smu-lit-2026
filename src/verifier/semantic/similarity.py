"""Cosine similarity, top-k retrieval, and the band classifier.

Everything here is deliberately small. The interesting decisions are in what the layers
ASK of these functions, not in the functions themselves -- see l3_alignment.py for why
L3 scores a margin between two cosines rather than a cosine.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import StrEnum
from operator import mul

Vector = list[float]


class Band(StrEnum):
    """Where a score falls relative to a layer's two thresholds."""

    FAIL = "fail"
    WARN = "warn"
    PASS = "pass"


@dataclass(frozen=True)
class Match:
    index: int
    score: float


def l2_normalise(vector: Vector) -> Vector:
    """Scale to unit length. A zero vector is returned unchanged rather than raising --
    an empty or all-stopword chunk is degenerate input, not a bug worth failing a run."""
    norm = math.sqrt(sum(v * v for v in vector))
    if norm == 0.0:
        return list(vector)
    return [v / norm for v in vector]


def dot(a: Vector, b: Vector) -> float:
    return sum(map(mul, a, b))


def cosine(a: Vector, b: Vector) -> float:
    """Cosine similarity.

    Vectors produced by :mod:`verifier.semantic.embed` are already unit length, so this
    reduces to a dot product; the norms are still computed because background vectors
    can arrive from a repo written by another process, and a silently unnormalised
    vector would inflate every score it touches.
    """
    if not a or not b or len(a) != len(b):
        return 0.0
    na = math.sqrt(sum(v * v for v in a))
    nb = math.sqrt(sum(v * v for v in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot(a, b) / (na * nb)


def top_k(query: Vector, candidates: list[Vector], k: int = 1) -> list[Match]:
    """The k most similar candidates, best first.

    Ranking, not scoring, is what these layers are built on: anisotropy compresses
    absolute cosines into a narrow band but leaves the ORDER intact
    (arXiv:2601.16907), so the reliable question is always "which of these is closest",
    never "is this above 0.7 in the abstract".
    """
    if not candidates or k <= 0:
        return []
    q = l2_normalise(query)
    scored = [Match(index=i, score=dot(q, l2_normalise(c))) for i, c in enumerate(candidates)]
    scored.sort(key=lambda m: (-m.score, m.index))
    return scored[:k]


def best_match(query: Vector, candidates: list[Vector]) -> Match | None:
    """``max cos(query, c) for c in candidates``, or None when there is nothing to score."""
    matches = top_k(query, candidates, k=1)
    return matches[0] if matches else None


def max_similarity(query: Vector, candidates: list[Vector], *, default: float = 0.0) -> float:
    match = best_match(query, candidates)
    return match.score if match is not None else default


def classify(score: float, *, fail_below: float, pass_at: float) -> Band:
    """Absolute-threshold band, used by L4."""
    if score < fail_below:
        return Band.FAIL
    if score < pass_at:
        return Band.WARN
    return Band.PASS


def classify_margin(margin: float, *, fail_at_or_below: float, pass_above: float) -> Band:
    """Margin band, used by L3.

    Note the boundary asymmetry against :func:`classify`: the FAIL test is ``<=`` and
    not ``<``, because a margin of exactly zero means the cited source is no better
    than an unrelated one, which is the definition of ungrounded and must not slip into
    WARN on a floating-point tie.
    """
    if margin <= fail_at_or_below:
        return Band.FAIL
    if margin <= pass_above:
        return Band.WARN
    return Band.PASS
