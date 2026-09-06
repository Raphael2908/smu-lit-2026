"""Unit tests for Harvey comparison statistics (no live JSONL, no network)."""

from __future__ import annotations

from scripts.harvey_stats import (
    cohen_kappa,
    kappa_bootstrap_ci,
    mcnemar_exact,
    metrics,
    wilson_ci,
)


def test_wilson_known_values() -> None:
    lo, hi = wilson_ci(95, 100)
    assert 0.887 < lo < 0.890
    assert 0.977 < hi < 0.979

    lo, hi = wilson_ci(10, 10)
    assert 0.722 < lo < 0.723
    assert hi == 1.0

    lo, hi = wilson_ci(0, 10)
    assert lo == 0.0
    assert 0.277 < hi < 0.278


def test_wilson_empty() -> None:
    assert wilson_ci(0, 0) is None


def test_kappa_perfect_and_unidentified() -> None:
    perfect = [("a", 0, 0), ("b", 1, 1), ("c", 0, 0), ("d", 1, 1)]
    assert cohen_kappa(perfect) == 1.0

    all_neg = [("a", 0, 0), ("b", 0, 0)]
    assert cohen_kappa(all_neg) is None


def test_kappa_chance() -> None:
    # Independent 50/50 mix that happens to match expected agreement.
    pairs = [("1", 0, 0), ("2", 0, 1), ("3", 1, 0), ("4", 1, 1)]
    assert cohen_kappa(pairs) == 0.0


def test_mcnemar_exact() -> None:
    none = mcnemar_exact(0, 0)
    assert none["p_value"] == 1.0
    assert none["bias"] == "none"

    strict = mcnemar_exact(0, 5)
    assert strict["bias"] == "strict"
    assert abs(strict["p_value"] - 0.0625) < 1e-12

    lenient = mcnemar_exact(5, 0)
    assert lenient["bias"] == "lenient"
    assert abs(lenient["p_value"] - 0.0625) < 1e-12

    balanced = mcnemar_exact(3, 3)
    assert balanced["bias"] == "balanced"
    assert balanced["p_value"] == 1.0


def test_metrics_agreement_ci_and_confusion() -> None:
    pairs = [
        ("1", 0, 0),
        ("2", 0, 0),
        ("3", 0, 0),
        ("4", 1, 1),
        ("5", 1, 0),
    ]
    m = metrics(pairs, n_boot=400, seed=1)
    assert m["n"] == 5
    assert m["tn"] == 3 and m["fp"] == 0 and m["fn"] == 1 and m["tp"] == 1
    assert m["agreement"] == 0.8
    assert m["agreement_ci"] is not None
    assert m["agreement_ci"][0] < 0.8 < m["agreement_ci"][1]
    assert m["kappa"] is not None
    assert m["mcnemar"]["bias"] == "strict"


def test_kappa_bootstrap_reproducible() -> None:
    pairs = [("1", 0, 0), ("2", 1, 1), ("3", 0, 1), ("4", 1, 0), ("5", 0, 0)]
    a = kappa_bootstrap_ci(pairs, n_boot=300, seed=7)
    b = kappa_bootstrap_ci(pairs, n_boot=300, seed=7)
    assert a == b
    assert a is not None
    assert a[0] <= a[1]
