"""Binary agreement statistics for Harvey system vs human GT.

Wilson score intervals apply to binomial proportions (agreement, class
prevalence, precision, recall). Cohen's kappa is required because raw
agreement is inflated when positives are sparse. Kappa CIs are bootstrap
percentile intervals. McNemar's exact test (binomial on discordant pairs)
checks systematic strictness, not closeness.
"""

from __future__ import annotations

import math
import random
from typing import Any, Literal

Z95 = 1.959963984540054
BOOTSTRAP_N = 10_000
BOOTSTRAP_SEED = 20260906
MIN_GROUP_CI_N = 10

Bias = Literal["strict", "lenient", "balanced", "none"]


def wilson_ci(successes: int, n: int, z: float = Z95) -> tuple[float, float] | None:
    """95% Wilson score interval for a binomial proportion."""
    if n <= 0:
        return None
    if successes < 0 or successes > n:
        raise ValueError(f"successes={successes} outside 0..{n}")
    phat = successes / n
    z2 = z * z
    denom = 1.0 + z2 / n
    center = (phat + z2 / (2.0 * n)) / denom
    margin = z * math.sqrt(phat * (1.0 - phat) / n + z2 / (4.0 * n * n)) / denom
    lower = 0.0 if successes == 0 else max(0.0, center - margin)
    upper = 1.0 if successes == n else min(1.0, center + margin)
    return (lower, upper)


def confusion(pairs: list[tuple[str, int, int]]) -> dict[str, int]:
    tn = fp = fn = tp = 0
    for _, human, system in pairs:
        if human == 0 and system == 0:
            tn += 1
        elif human == 0 and system == 1:
            fp += 1
        elif human == 1 and system == 0:
            fn += 1
        else:
            tp += 1
    return {"tn": tn, "fp": fp, "fn": fn, "tp": tp}


def _safe_div(num: float, den: float) -> float | None:
    if den == 0:
        return None
    return num / den


def cohen_kappa(pairs: list[tuple[str, int, int]]) -> float | None:
    """Cohen's kappa, or None when only one class is observed (kappa unidentified)."""
    n = len(pairs)
    if n == 0:
        return None
    agree = sum(1 for _, h, s in pairs if h == s) / n
    p_h1 = sum(1 for _, h, _ in pairs if h == 1) / n
    p_s1 = sum(1 for _, _, s in pairs if s == 1) / n
    pe = p_h1 * p_s1 + (1.0 - p_h1) * (1.0 - p_s1)
    if math.isclose(pe, 1.0):
        return None
    return (agree - pe) / (1.0 - pe)


def _percentile(sorted_vals: list[float], q: float) -> float:
    if not sorted_vals:
        raise ValueError("empty sample")
    pos = q * (len(sorted_vals) - 1)
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))
    if lo == hi:
        return sorted_vals[lo]
    weight = pos - lo
    return sorted_vals[lo] * (1.0 - weight) + sorted_vals[hi] * weight


def kappa_bootstrap_ci(
    pairs: list[tuple[str, int, int]],
    *,
    n_boot: int = BOOTSTRAP_N,
    seed: int = BOOTSTRAP_SEED,
) -> tuple[float, float] | None:
    """95% percentile bootstrap CI for Cohen's kappa."""
    n = len(pairs)
    if n == 0 or cohen_kappa(pairs) is None:
        return None
    rng = random.Random(seed)
    samples: list[float] = []
    for _ in range(n_boot):
        draw = [pairs[rng.randrange(n)] for _ in range(n)]
        value = cohen_kappa(draw)
        if value is not None:
            samples.append(value)
    if len(samples) < max(100, n_boot // 20):
        return None
    samples.sort()
    return (_percentile(samples, 0.025), _percentile(samples, 0.975))


def _binom_cdf_le(k: int, n: int) -> float:
    """P(X <= k) for X ~ Binomial(n, 0.5)."""
    if n <= 0:
        return 1.0
    if k < 0:
        return 0.0
    if k >= n:
        return 1.0
    return sum(math.comb(n, i) for i in range(k + 1)) / float(2**n)


def mcnemar_exact(fp: int, fn: int) -> dict[str, Any]:
    """Exact McNemar test: two-sided binomial on discordant pairs, p=0.5."""
    n_disc = fp + fn
    if n_disc == 0:
        p_value = 1.0
        bias: Bias = "none"
    else:
        p_value = min(1.0, 2.0 * _binom_cdf_le(min(fp, fn), n_disc))
        if fp > fn:
            bias = "lenient"
        elif fn > fp:
            bias = "strict"
        else:
            bias = "balanced"
    return {
        "n_discordant": n_disc,
        "fp": fp,
        "fn": fn,
        "p_value": p_value,
        "bias": bias,
    }


def _prop(
    successes: int, n: int, *, with_ci: bool
) -> tuple[float | None, tuple[float, float] | None]:
    rate = _safe_div(successes, n)
    ci = wilson_ci(successes, n) if with_ci and n > 0 else None
    return rate, ci


def metrics(
    pairs: list[tuple[str, int, int]],
    *,
    with_ci: bool = True,
    n_boot: int = BOOTSTRAP_N,
    seed: int = BOOTSTRAP_SEED,
) -> dict[str, Any]:
    n = len(pairs)
    cm = confusion(pairs)
    tp, fp, fn, tn = cm["tp"], cm["fp"], cm["fn"], cm["tn"]
    agreement, agreement_ci = _prop(tp + tn, n, with_ci=with_ci)
    precision, precision_ci = _prop(tp, tp + fp, with_ci=with_ci)
    recall, recall_ci = _prop(tp, tp + fn, with_ci=with_ci)
    human_pos, human_pos_ci = _prop(tp + fn, n, with_ci=with_ci)
    system_pos, system_pos_ci = _prop(tp + fp, n, with_ci=with_ci)
    f1 = None
    if precision is not None and recall is not None and (precision + recall) > 0:
        f1 = 2 * precision * recall / (precision + recall)
    kappa = cohen_kappa(pairs)
    kappa_ci = kappa_bootstrap_ci(pairs, n_boot=n_boot, seed=seed) if with_ci else None
    mcnemar = mcnemar_exact(fp, fn)
    return {
        "n": n,
        **cm,
        "agreement": agreement,
        "agreement_ci": agreement_ci,
        "kappa": kappa,
        "kappa_ci": kappa_ci,
        "precision": precision,
        "precision_ci": precision_ci,
        "recall": recall,
        "recall_ci": recall_ci,
        "f1": f1,
        "human_pos_rate": human_pos,
        "human_pos_ci": human_pos_ci,
        "system_pos_rate": system_pos,
        "system_pos_ci": system_pos_ci,
        "mcnemar": mcnemar,
    }
