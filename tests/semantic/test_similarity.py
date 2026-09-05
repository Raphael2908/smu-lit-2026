"""Cosine, top-k and the two band classifiers."""

from __future__ import annotations

import math

import pytest

from verifier.semantic.similarity import (
    Band,
    best_match,
    classify,
    classify_margin,
    cosine,
    l2_normalise,
    max_similarity,
    top_k,
)


def test_l2_normalise_produces_unit_vectors_and_tolerates_zero():
    assert math.isclose(sum(v * v for v in l2_normalise([3.0, 4.0])), 1.0)
    assert l2_normalise([0.0, 0.0]) == [0.0, 0.0]


@pytest.mark.parametrize(
    "a, b, expected",
    [
        ([1.0, 0.0], [1.0, 0.0], 1.0),
        ([1.0, 0.0], [0.0, 1.0], 0.0),
        ([1.0, 0.0], [-1.0, 0.0], -1.0),
        ([2.0, 0.0], [5.0, 0.0], 1.0),  # magnitude-invariant
    ],
)
def test_cosine(a, b, expected):
    assert math.isclose(cosine(a, b), expected, abs_tol=1e-9)


@pytest.mark.parametrize("a, b", [([], [1.0]), ([1.0], []), ([1.0, 0.0], [1.0]), ([0.0], [1.0])])
def test_cosine_degenerate_input_scores_zero_rather_than_raising(a, b):
    assert cosine(a, b) == 0.0


def test_top_k_ranks_best_first():
    query = [1.0, 0.0]
    candidates = [[0.0, 1.0], [1.0, 0.0], [0.7, 0.7]]
    matches = top_k(query, candidates, k=2)
    assert [m.index for m in matches] == [1, 2]
    assert matches[0].score > matches[1].score


def test_best_match_and_max_similarity_on_an_empty_corpus():
    assert best_match([1.0, 0.0], []) is None
    assert max_similarity([1.0, 0.0], []) == 0.0
    assert top_k([1.0, 0.0], [[1.0, 0.0]], k=0) == []


@pytest.mark.parametrize(
    "score, expected",
    [(0.49, Band.FAIL), (0.50, Band.WARN), (0.69, Band.WARN), (0.70, Band.PASS)],
)
def test_absolute_bands_used_by_l4(score, expected):
    assert classify(score, fail_below=0.50, pass_at=0.70) is expected


@pytest.mark.parametrize(
    "margin, expected",
    [
        (-0.5, Band.FAIL),
        (0.0, Band.FAIL),  # no better than an unrelated judgment
        (0.02, Band.FAIL),  # boundary is <=, not <
        (0.021, Band.WARN),
        (0.08, Band.WARN),
        (0.081, Band.PASS),
    ],
)
def test_margin_bands_used_by_l3(margin, expected):
    assert classify_margin(margin, fail_at_or_below=0.02, pass_above=0.08) is expected
