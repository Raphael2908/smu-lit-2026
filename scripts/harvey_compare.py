"""Join Harvey L5 harness output to the human GT CSV and write a report.

    uv run python -m scripts.harvey_compare
    uv run python -m scripts.harvey_compare --png-dpi 600
    uv run python -m scripts.harvey_benchmark --compare-only

Human labels are sparse positives (Correctness 12/102, Groundness 11/102), so
this report leads with kappa and the confusion matrix, not raw accuracy.

Groundness is compared only on rows where L5 actually ran. L4-fail rows set
Correctness = 0 and omit Groundness.
"""

from __future__ import annotations

import argparse
import csv
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from scripts.harvey_figures import DEFAULT_PNG_DPI, write_figures
from scripts.harvey_stats import MIN_GROUP_CI_N, metrics

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_RESULTS = ROOT / "benchmark-output" / "harvey_l5_results.jsonl"
DEFAULT_GT = ROOT / "benchmark-input" / "harvey_ground_truth.csv"
GT_N = 102
GT_CORRECTNESS_POS = 12
GT_GROUNDNESS_POS = 11


def load_results(path: Path) -> dict[str, dict[str, Any]]:
    if not path.exists():
        raise SystemExit(f"No results file at {path}")
    by_id: dict[str, dict[str, Any]] = {}
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                # Last line may be incomplete if a scorer is still appending.
                continue
            qid = row.get("question_id")
            if qid is None:
                continue
            by_id[str(qid)] = row
    return by_id


def load_gt(path: Path) -> dict[str, dict[str, int]]:
    with path.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    needed = ("Question ID", "Correctness", "Groundness")
    missing = [name for name in needed if name not in (rows[0] if rows else {})]
    if missing:
        raise SystemExit(f"{path} missing columns: {', '.join(missing)}")
    out: dict[str, dict[str, int]] = {}
    for row in rows:
        out[row["Question ID"].strip()] = {
            "correctness": int(row["Correctness"]),
            "groundness": int(row["Groundness"]),
        }
    return out


def _bit(value: Any) -> int | None:
    if value in (0, 1):
        return int(value)
    if value in ("0", "1"):
        return int(value)
    return None


def paired(
    results: dict[str, dict[str, Any]],
    gt: dict[str, dict[str, int]],
    *,
    human_key: str,
    system_key: str,
) -> list[tuple[str, int, int]]:
    pairs: list[tuple[str, int, int]] = []
    for qid, human in gt.items():
        sys_row = results.get(qid)
        if sys_row is None:
            continue
        sys_bit = _bit(sys_row.get(system_key))
        if sys_bit is None:
            continue
        pairs.append((qid, human[human_key], sys_bit))
    return pairs


def group_id(qid: str) -> str:
    return qid.split("-", 1)[0]


def per_group(
    pairs: list[tuple[str, int, int]],
) -> list[tuple[str, dict[str, Any]]]:
    buckets: dict[str, list[tuple[str, int, int]]] = defaultdict(list)
    for item in pairs:
        buckets[group_id(item[0])].append(item)
    out: list[tuple[str, dict[str, Any]]] = []
    for gid, items in sorted(buckets.items(), key=lambda x: int(x[0])):
        out.append((gid, metrics(items, with_ci=len(items) >= MIN_GROUP_CI_N)))
    return out


def _counts(results: dict[str, dict[str, Any]], gt: dict[str, dict[str, int]]) -> dict[str, int]:
    statuses = Counter(str(row.get("status") or "") for row in results.values())
    return {
        "gt_rows": len(gt),
        "result_rows": len(results),
        "complete_correctness": sum(
            1 for qid in gt if _bit(results.get(qid, {}).get("sys_correctness")) is not None
        ),
        "complete_groundness": sum(
            1 for qid in gt if _bit(results.get(qid, {}).get("sys_groundness")) is not None
        ),
        "l4_failed": sum(1 for row in results.values() if row.get("l4_failed")),
        "l5_ran": sum(1 for row in results.values() if row.get("l5_ran")),
        "errors": sum(
            1 for row in results.values() if row.get("status") == "error" or row.get("error")
        ),
        **{f"status_{k or 'blank'}": v for k, v in statuses.items()},
    }


def _fmt(value: float | None, digits: int = 3) -> str:
    if value is None:
        return "n/a"
    return f"{value:.{digits}f}"


def _fmt_ci(ci: tuple[float, float] | None, digits: int = 3) -> str:
    if ci is None:
        return "n/a"
    return f"{ci[0]:.{digits}f}–{ci[1]:.{digits}f}"


def _fmt_est_ci(value: float | None, ci: tuple[float, float] | None, digits: int = 3) -> str:
    if value is None:
        return "n/a"
    if ci is None:
        return _fmt(value, digits)
    return f"{value:.{digits}f} ({_fmt_ci(ci, digits)})"


def _md_table(headers: list[str], rows: list[list[str]]) -> str:
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    for row in rows:
        lines.append("| " + " | ".join(row) + " |")
    return "\n".join(lines)


def _bias_phrase(m: dict[str, Any]) -> str:
    bias = m["mcnemar"]["bias"]
    p_value = m["mcnemar"]["p_value"]
    p_txt = f"McNemar exact p = {_fmt(p_value)}"
    sig = p_value < 0.05
    if bias == "none":
        return f"no discordant pairs ({p_txt})"
    if bias == "balanced":
        return f"discordant pairs are balanced; no directional bias ({p_txt})"
    if bias == "strict":
        direction = "directionally stricter (more FN than FP)"
    else:
        direction = "directionally more lenient (more FP than FN)"
    if sig:
        return f"{direction}; the imbalance is significant ({p_txt})"
    return f"{direction}, but McNemar does not reject symmetry ({p_txt})"


def _closeness_claim(m: dict[str, Any], *, label: str) -> str:
    agr = m["agreement"]
    agr_ci = m["agreement_ci"]
    kappa = m["kappa"]
    if agr is None:
        return f"{label}: no paired rows."
    if agr_ci is not None and agr_ci[0] >= 0.95 and kappa is not None and kappa >= 0.80:
        return (
            f"{label}: the Wilson lower bound is ≥ 0.95 and κ ≥ 0.80, "
            "which supports a ~95% comparability claim on this snapshot."
        )
    if agr_ci is not None and agr_ci[0] >= 0.95 and kappa is None:
        return (
            f"{label}: raw agreement has a Wilson lower bound ≥ 0.95, but κ is unidentified "
            "(only one class observed). That is not evidence of 95% comparability."
        )
    parts = [f"{label}: do not claim ~95% comparability on this snapshot."]
    if agr_ci is not None and agr_ci[1] < 0.95:
        parts.append("The entire Wilson 95% CI for agreement lies below 0.95.")
    elif agr_ci is not None and agr_ci[0] < 0.95 <= agr_ci[1]:
        parts.append("The Wilson 95% CI for agreement includes 0.95 but the lower bound does not.")
    elif agr is not None and agr >= 0.95:
        parts.append("The point estimate of agreement is ≥ 0.95, but the interval is not.")
    if kappa is None:
        parts.append("κ is unidentified because only one class appears.")
    elif kappa < 0.40:
        parts.append(
            f"κ = {_fmt(kappa)} is low: raw agreement is inflated by the many human-negative rows."
        )
    elif kappa < 0.60:
        parts.append(f"κ = {_fmt(kappa)} is only moderate.")
    return " ".join(parts)


def _headline_block(label: str, m: dict[str, Any]) -> str:
    return (
        f"**{label}** (n = {m['n']} of {GT_N}): "
        f"agreement {_fmt_est_ci(m['agreement'], m['agreement_ci'])}; "
        f"Cohen's κ {_fmt_est_ci(m['kappa'], m['kappa_ci'])}."
    )


def _coverage_note(counts: dict[str, int]) -> str:
    n_corr = counts["complete_correctness"]
    n_ground = counts["complete_groundness"]
    if counts["result_rows"] >= GT_N and n_corr >= GT_N:
        note = f"Scoring is complete: {n_corr}/{GT_N} paired Correctness rows."
        if n_ground < GT_N:
            note += (
                f" Groundness is {n_ground}/{GT_N} because L4-fail rows omit Groundness "
                f"({counts['l4_failed']} such row(s))."
            )
        return note
    return (
        f"This snapshot is **partial**: {n_corr}/{GT_N} paired Correctness rows and "
        f"{n_ground}/{GT_N} paired Groundness rows. Groundness is omitted when L5 was skipped."
    )


def render_markdown(
    *,
    correctness: dict[str, Any],
    groundness: dict[str, Any],
    correctness_groups: list[tuple[str, dict[str, Any]]],
    groundness_groups: list[tuple[str, dict[str, Any]]],
    counts: dict[str, int],
    figure_rel: list[str],
    png_dpi: int,
) -> str:
    summary_headers = [
        "Dimension",
        "n",
        "Agreement (95% Wilson CI)",
        "Cohen's κ (95% bootstrap CI)",
        "Precision (95% Wilson CI)",
        "Recall (95% Wilson CI)",
        "F1",
        "Human pos. (95% Wilson CI)",
        "System pos. (95% Wilson CI)",
        "TN/FP/FN/TP",
    ]
    summary_rows = [
        [
            label,
            str(m["n"]),
            _fmt_est_ci(m["agreement"], m["agreement_ci"]),
            _fmt_est_ci(m["kappa"], m["kappa_ci"]),
            _fmt_est_ci(m["precision"], m["precision_ci"]),
            _fmt_est_ci(m["recall"], m["recall_ci"]),
            _fmt(m["f1"]),
            _fmt_est_ci(m["human_pos_rate"], m["human_pos_ci"]),
            _fmt_est_ci(m["system_pos_rate"], m["system_pos_ci"]),
            f"{m['tn']}/{m['fp']}/{m['fn']}/{m['tp']}",
        ]
        for label, m in (("Correctness", correctness), ("Groundness", groundness))
    ]

    def group_rows(groups: list[tuple[str, dict[str, Any]]]) -> list[list[str]]:
        rows: list[list[str]] = []
        for gid, m in groups:
            if m["n"] < MIN_GROUP_CI_N:
                agree = _fmt(m["agreement"])
                kappa = _fmt(m["kappa"])
            else:
                agree = _fmt_est_ci(m["agreement"], m["agreement_ci"])
                kappa = _fmt_est_ci(m["kappa"], m["kappa_ci"])
            rows.append(
                [
                    gid,
                    str(m["n"]),
                    agree,
                    kappa,
                    _fmt(m["human_pos_rate"]),
                    _fmt(m["system_pos_rate"]),
                ]
            )
        return rows

    figure_notes = {
        "fig1_closeness.png": (
            "**Figure 1.** Agreement and Cohen's κ for the automated grader versus "
            "human GT, with 95% confidence intervals (Wilson score for agreement; "
            "percentile bootstrap for κ). The dashed line marks 0.95 agreement. "
            "κ is the primary closeness measure because human positives are sparse."
        ),
        "fig2_confusion.png": (
            "**Figure 2.** Confusion matrices. Each cell shows the count and the "
            "row percentage (share of that human label). Color is ColorBrewer Blues, "
            "scaled separately in each panel."
        ),
        "fig3_strictness.png": (
            "**Figure 3.** Human versus automated positive rates with 95% Wilson "
            "score intervals. Directional strictness is read from these rates and "
            "from McNemar's exact test on discordant pairs."
        ),
    }

    chart_lines: list[str] = []
    for rel in figure_rel:
        name = Path(rel).name
        if name in figure_notes:
            chart_lines.append(figure_notes[name])
            chart_lines.append("")
        chart_lines.append(f"![{rel}]({rel})")
        chart_lines.append("")
        stem = Path(rel).with_suffix("")
        chart_lines.append(
            f"Vector: `{stem.as_posix()}.pdf` (paper), `{stem.as_posix()}.svg`. "
            f"PNG exported at {png_dpi} dpi."
        )
        chart_lines.append("")

    body = [
        "# Harvey system vs human GT",
        "",
        "One-off mapping (harness only, production gate unchanged):",
        "",
        "- **Correctness** = L5 `correctness`, except L4 FAIL → 0 and L5 skipped.",
        "- **Groundness** = L5 `material_completeness` only when L5 ran.",
        "",
        f"Scored {counts['result_rows']} / {counts['gt_rows']} GT rows. "
        f"L4-fail (no L5): {counts['l4_failed']}. L5 ran: {counts['l5_ran']}. "
        f"Rows with an error string: {counts['errors']}.",
        "",
        _coverage_note(counts),
        "",
        "## Headline",
        "",
        _headline_block("Correctness", correctness),
        "",
        _headline_block("Groundness", groundness),
        "",
        (
            "Correctness strictness: the system is "
            + f"{_bias_phrase(correctness)}. Human positive rate "
            + f"{_fmt_est_ci(correctness['human_pos_rate'], correctness['human_pos_ci'])}; "
            + f"system {_fmt_est_ci(correctness['system_pos_rate'], correctness['system_pos_ci'])}."
        ),
        "",
        (
            "Groundness strictness: the system is "
            + f"{_bias_phrase(groundness)}. Human positive rate "
            + f"{_fmt_est_ci(groundness['human_pos_rate'], groundness['human_pos_ci'])}; "
            + f"system {_fmt_est_ci(groundness['system_pos_rate'], groundness['system_pos_ci'])}."
        ),
        "",
        _closeness_claim(correctness, label="Correctness"),
        "",
        _closeness_claim(groundness, label="Groundness"),
        "",
        "## Summary",
        "",
        _md_table(summary_headers, summary_rows),
        "",
        (
            f"Human positives are sparse in the full GT (Correctness {GT_CORRECTNESS_POS}/{GT_N}, "
            f"Groundness {GT_GROUNDNESS_POS}/{GT_N}). Agreement can look high while the system "
            "rarely predicts 1; κ and the confusion counts are the useful closeness numbers."
        ),
        "",
        "## Confusion matrices",
        "",
        "### Correctness",
        "",
        _md_table(
            ["", "System 0", "System 1"],
            [
                ["Human 0", str(correctness["tn"]), str(correctness["fp"])],
                ["Human 1", str(correctness["fn"]), str(correctness["tp"])],
            ],
        ),
        "",
        "### Groundness (L5 rows only)",
        "",
        _md_table(
            ["", "System 0", "System 1"],
            [
                ["Human 0", str(groundness["tn"]), str(groundness["fp"])],
                ["Human 1", str(groundness["fn"]), str(groundness["tp"])],
            ],
        ),
        "",
        "## Per-group agreement",
        "",
        f"Wilson / bootstrap CIs are shown only for groups with n ≥ {MIN_GROUP_CI_N}. "
        "Smaller groups report the point estimate only.",
        "",
        "### Correctness by Harvey group",
        "",
        _md_table(
            ["Group", "n", "Agreement", "Kappa", "Human pos.", "System pos."],
            group_rows(correctness_groups),
        ),
        "",
        "### Groundness by Harvey group",
        "",
        _md_table(
            ["Group", "n", "Agreement", "Kappa", "Human pos.", "System pos."],
            group_rows(groundness_groups),
        ),
        "",
        "## Methods",
        "",
        "- **Agreement, positive rates, precision, recall:** binomial proportions with "
        "95% Wilson score intervals.",
        "- **Cohen's κ:** chance-corrected agreement. 95% CI is a percentile bootstrap "
        "(B = 10,000, seed = 20260906). κ is reported as unidentified "
        "when only one class is observed (expected agreement = 1).",
        "- **McNemar (exact):** two-sided binomial test on discordant pairs "
        "(system-only-positive vs human-only-positive), as a secondary check for "
        "systematic strictness, not as a closeness metric.",
        "- **Groundness sample:** L5-scored rows only. L4 FAIL sets Correctness = 0 "
        "and omits Groundness.",
        "",
        "## Figures",
        "",
        *chart_lines,
    ]
    return "\n".join(body).rstrip() + "\n"


def write_report(
    results_path: Path,
    gt_path: Path,
    *,
    png_dpi: int = DEFAULT_PNG_DPI,
) -> Path:
    results = load_results(results_path)
    gt = load_gt(gt_path)
    corr_pairs = paired(results, gt, human_key="correctness", system_key="sys_correctness")
    ground_pairs = paired(results, gt, human_key="groundness", system_key="sys_groundness")
    correctness = metrics(corr_pairs)
    groundness = metrics(ground_pairs)
    corr_groups = per_group(corr_pairs)
    ground_groups = per_group(ground_pairs)
    counts = _counts(results, gt)

    out_dir = results_path.parent
    fig_dir = out_dir / "figures"
    out_dir.mkdir(parents=True, exist_ok=True)
    written = write_figures(
        fig_dir,
        correctness=correctness,
        groundness=groundness,
        png_dpi=png_dpi,
    )
    figure_rel = [
        path.relative_to(out_dir).as_posix()
        for path in written
        if path.suffix == ".png"
    ]

    report_path = out_dir / "harvey_comparison.md"
    report_path.write_text(
        render_markdown(
            correctness=correctness,
            groundness=groundness,
            correctness_groups=corr_groups,
            groundness_groups=ground_groups,
            counts=counts,
            figure_rel=figure_rel,
            png_dpi=png_dpi,
        ),
        encoding="utf-8",
    )
    print(f"Wrote {report_path}")
    for path in written:
        print(f"Wrote {path}")
    print(
        f"correctness n={correctness['n']} "
        f"agree={_fmt_est_ci(correctness['agreement'], correctness['agreement_ci'])} "
        f"kappa={_fmt_est_ci(correctness['kappa'], correctness['kappa_ci'])} | "
        f"groundness n={groundness['n']} "
        f"agree={_fmt_est_ci(groundness['agreement'], groundness['agreement_ci'])} "
        f"kappa={_fmt_est_ci(groundness['kappa'], groundness['kappa_ci'])}"
    )
    return report_path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results", type=Path, default=DEFAULT_RESULTS)
    parser.add_argument("--gt", type=Path, default=DEFAULT_GT)
    parser.add_argument(
        "--png-dpi",
        type=int,
        default=DEFAULT_PNG_DPI,
        help="PNG resolution (default 600; use 1200 for very large slides). PDF/SVG stay vector.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.png_dpi < 72:
        raise SystemExit("--png-dpi must be at least 72")
    write_report(args.results, args.gt, png_dpi=args.png_dpi)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
