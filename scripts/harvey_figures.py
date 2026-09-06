"""Publication figures for Harvey automated vs human grader comparison.

Exports PDF (vector), SVG (vector), and PNG at a slideshow-safe DPI
(default 600). Palette is Okabe–Ito / ColorBrewer; no decorative UI styling.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")

import matplotlib.font_manager as fm
import matplotlib.pyplot as plt
from matplotlib.colors import LinearSegmentedColormap

# Okabe–Ito (https://jfly.uni-koeln.de/color/)
OKABE_BLACK = "#000000"
OKABE_ORANGE = "#E69F00"
OKABE_BLUE = "#0072B2"
OKABE_VERMILION = "#D55E00"
SPINE = "#333333"
MUTED = "#666666"
GRID = "#D0D0D0"

# ColorBrewer Blues (print-safe sequential)
CB_BLUES = LinearSegmentedColormap.from_list(
    "cb_blues",
    [
        "#f7fbff",
        "#deebf7",
        "#c6dbef",
        "#9ecae1",
        "#6baed6",
        "#4292c6",
        "#2171b5",
        "#084594",
    ],
)

DEFAULT_PNG_DPI = 600
FIGURE_WIDTH = 6.4  # 6.4 in × 600 dpi = 3840 px (4K slide width)


def _font_family() -> str:
    available = {item.name for item in fm.fontManager.ttflist}
    for name in ("Times New Roman", "DejaVu Serif", "DejaVu Sans"):
        if name in available:
            return name
    return "DejaVu Sans"


def apply_style() -> None:
    family = _font_family()
    plt.rcParams.update(
        {
            "font.family": family,
            "font.size": 10,
            "axes.labelsize": 10,
            "axes.titlesize": 11,
            "xtick.labelsize": 9,
            "ytick.labelsize": 9,
            "legend.fontsize": 9,
            "axes.linewidth": 0.6,
            "axes.edgecolor": SPINE,
            "axes.facecolor": "white",
            "axes.spines.top": False,
            "axes.spines.right": False,
            "axes.grid": False,
            "figure.facecolor": "white",
            "figure.edgecolor": "white",
            "savefig.facecolor": "white",
            "savefig.edgecolor": "none",
            "pdf.fonttype": 42,
            "ps.fonttype": 42,
            "svg.fonttype": "none",
            "legend.frameon": False,
            "lines.solid_capstyle": "butt",
        }
    )


def _save(fig: plt.Figure, stem: Path, png_dpi: int) -> list[Path]:
    stem.parent.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for suffix, kwargs in (
        (".pdf", {"format": "pdf"}),
        (".svg", {"format": "svg"}),
        (".png", {"format": "png", "dpi": png_dpi}),
    ):
        path = stem.with_suffix(suffix)
        fig.savefig(path, bbox_inches="tight", pad_inches=0.03, **kwargs)
        written.append(path)
    plt.close(fig)
    return written


def _ci(metric: dict[str, Any], key: str) -> tuple[float, float] | None:
    value = metric.get(key)
    if value is None or len(value) != 2:
        return None
    return float(value[0]), float(value[1])


def fig1_closeness(
    correctness: dict[str, Any],
    groundness: dict[str, Any],
) -> plt.Figure:
    """Forest plot of agreement (Wilson) and kappa (bootstrap) with 95% CIs."""
    rows: list[tuple[str, str, float | None, tuple[float, float] | None, str]] = [
        (
            "Correctness",
            "Agreement",
            correctness.get("agreement"),
            _ci(correctness, "agreement_ci"),
            "agreement",
        ),
        (
            "Correctness",
            "Cohen's κ",
            correctness.get("kappa"),
            _ci(correctness, "kappa_ci"),
            "kappa",
        ),
        (
            "Groundness",
            "Agreement",
            groundness.get("agreement"),
            _ci(groundness, "agreement_ci"),
            "agreement",
        ),
        (
            "Groundness",
            "Cohen's κ",
            groundness.get("kappa"),
            _ci(groundness, "kappa_ci"),
            "kappa",
        ),
    ]
    y_positions = [3.25, 2.55, 0.85, 0.15]
    fig, ax = plt.subplots(figsize=(FIGURE_WIDTH, 3.55))
    ax.axvline(0.0, color=MUTED, lw=0.6, ls="-")
    ax.axvline(0.95, color=MUTED, lw=0.7, ls=(0, (3, 2)))
    ax.text(
        0.95,
        3.72,
        "0.95",
        ha="center",
        va="bottom",
        fontsize=8,
        color=MUTED,
        clip_on=False,
    )

    color_for = {"agreement": OKABE_BLUE, "kappa": OKABE_VERMILION}
    marker_for = {"agreement": "o", "kappa": "s"}
    for y, (_dim, _name, estimate, ci, kind) in zip(y_positions, rows, strict=True):
        color = color_for[kind]
        if estimate is None:
            ax.plot(0.0, y, marker="x", color=MUTED, ms=6, mew=0.8)
            ax.text(1.08, y, "undefined", va="center", ha="left", fontsize=8, color=MUTED)
            continue
        if ci is not None:
            lo, hi = ci
            ax.plot([lo, hi], [y, y], color=color, lw=1.4, solid_capstyle="butt")
            ax.plot([lo, lo], [y - 0.07, y + 0.07], color=color, lw=1.2)
            ax.plot([hi, hi], [y - 0.07, y + 0.07], color=color, lw=1.2)
            label = f"{estimate:.3f}  [{lo:.3f}, {hi:.3f}]"
        else:
            label = f"{estimate:.3f}"
        ax.plot(
            estimate,
            y,
            marker=marker_for[kind],
            color=color,
            ms=6.5,
            mec=OKABE_BLACK,
            mew=0.4,
            zorder=3,
        )
        ax.text(1.08, y, label, va="center", ha="left", fontsize=8, color=OKABE_BLACK)

    ax.set_yticks(y_positions)
    ax.set_yticklabels(
        [
            "Correctness — Agreement",
            "Correctness — Cohen's κ",
            "Groundness — Agreement",
            "Groundness — Cohen's κ",
        ]
    )
    ax.set_xlabel("Estimate (95% CI: Wilson for agreement; bootstrap for κ)")
    ax.set_xlim(-0.25, 1.72)
    ax.set_ylim(-0.35, 3.95)
    ax.axhline(1.70, color=GRID, lw=0.6)
    ax.set_title(
        "Automated vs human closeness (agreement and Cohen's κ)",
        pad=8,
    )
    handles = [
        plt.Line2D(
            [0],
            [0],
            color=OKABE_BLUE,
            marker="o",
            ms=5.5,
            mec=OKABE_BLACK,
            mew=0.4,
            lw=1.3,
            label="Agreement (Wilson 95% CI)",
        ),
        plt.Line2D(
            [0],
            [0],
            color=OKABE_VERMILION,
            marker="s",
            ms=5.5,
            mec=OKABE_BLACK,
            mew=0.4,
            lw=1.3,
            label="Cohen's κ (bootstrap 95% CI)",
        ),
    ]
    ax.legend(handles=handles, loc="lower right", bbox_to_anchor=(1.0, -0.02))
    return fig


def _row_rates(tn: int, fp: int, fn: int, tp: int) -> list[list[str]]:
    neg = tn + fp
    pos = fn + tp
    def cell(count: int, row_n: int) -> str:
        if row_n == 0:
            return f"{count}\n(—)"
        return f"{count}\n({count / row_n:.0%})"

    return [
        [cell(tn, neg), cell(fp, neg)],
        [cell(fn, pos), cell(tp, pos)],
    ]


def fig2_confusion(
    correctness: dict[str, Any],
    groundness: dict[str, Any],
) -> plt.Figure:
    """Two-panel confusion heatmaps with cell counts and row percentages."""
    fig, axes = plt.subplots(1, 2, figsize=(FIGURE_WIDTH, 3.35))
    panels = (
        (axes[0], correctness, "Correctness"),
        (axes[1], groundness, "Groundness (L5-scored rows)"),
    )
    for ax, metric, title in panels:
        tn, fp, fn, tp = metric["tn"], metric["fp"], metric["fn"], metric["tp"]
        mat = [[tn, fp], [fn, tp]]
        vmax = max(tn, fp, fn, tp, 1)
        im = ax.imshow(mat, cmap=CB_BLUES, vmin=0, vmax=vmax, aspect="equal")
        labels = _row_rates(tn, fp, fn, tp)
        for i in range(2):
            for j in range(2):
                count = mat[i][j]
                # Mid-ramp cells need dark text; the top two ColorBrewer Blues stay light.
                text_color = OKABE_BLACK if count <= 0.55 * vmax else "white"
                ax.text(
                    j,
                    i,
                    labels[i][j],
                    ha="center",
                    va="center",
                    fontsize=9,
                    color=text_color,
                    linespacing=1.25,
                )
        ax.set_xticks([0, 1], ["System 0", "System 1"])
        ax.set_yticks([0, 1], ["Human 0", "Human 1"])
        ax.set_xlabel("Automated label")
        ax.set_ylabel("Human GT label")
        ax.set_title(f"{title}  (n = {metric['n']})", pad=8)
        ax.spines["top"].set_visible(True)
        ax.spines["right"].set_visible(True)
        for spine in ax.spines.values():
            spine.set_linewidth(0.6)
            spine.set_color(SPINE)
        ax.tick_params(length=0)
        cbar = fig.colorbar(im, ax=ax, fraction=0.046, pad=0.04)
        cbar.set_label("Count", fontsize=8)
        cbar.ax.tick_params(labelsize=8)
        cbar.outline.set_linewidth(0.6)
    fig.suptitle(
        "Confusion of automated vs human binary labels (count; row %)",
        fontsize=11,
        y=1.02,
    )
    fig.tight_layout()
    return fig


def fig3_strictness(
    correctness: dict[str, Any],
    groundness: dict[str, Any],
) -> plt.Figure:
    """Human vs system positive rates with Wilson 95% CIs."""
    fig, ax = plt.subplots(figsize=(FIGURE_WIDTH, 3.45))
    categories = ["Correctness", "Groundness"]
    series = [
        (
            "Human GT",
            OKABE_BLUE,
            [
                (correctness.get("human_pos_rate"), _ci(correctness, "human_pos_ci")),
                (groundness.get("human_pos_rate"), _ci(groundness, "human_pos_ci")),
            ],
        ),
        (
            "Automated grader",
            OKABE_ORANGE,
            [
                (correctness.get("system_pos_rate"), _ci(correctness, "system_pos_ci")),
                (groundness.get("system_pos_rate"), _ci(groundness, "system_pos_ci")),
            ],
        ),
    ]
    x = [0.0, 1.15]
    width = 0.38
    offsets = (-width / 2, width / 2)
    for s_idx, (name, color, values) in enumerate(series):
        xs = [xi + offsets[s_idx] for xi in x]
        heights = [0.0 if est is None else est for est, _ in values]
        ax.bar(
            xs,
            heights,
            width=width,
            color=color,
            edgecolor=OKABE_BLACK,
            linewidth=0.5,
            label=name,
            zorder=2,
        )
        for xi, (est, ci) in zip(xs, values, strict=True):
            if est is None or ci is None:
                continue
            lo, hi = ci
            ax.errorbar(
                xi,
                est,
                yerr=[[max(0.0, est - lo)], [max(0.0, hi - est)]],
                fmt="none",
                ecolor=OKABE_BLACK,
                elinewidth=0.9,
                capsize=3.0,
                capthick=0.9,
                zorder=3,
            )
    uppers = [
        ci[1]
        for _name, _color, values in series
        for est, ci in values
        if ci is not None
    ]
    ymax = min(1.0, max(0.30, max(uppers, default=1.0) * 1.25))
    ax.set_xticks(x, categories)
    ax.set_ylabel("Positive rate (95% Wilson CI)")
    ax.set_ylim(0.0, ymax)
    ax.set_xlim(-0.5, 1.65)
    ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda v, _: f"{v:.1f}"))
    ax.set_title("Human vs automated positive rates (strictness)", pad=8)
    ax.legend(loc="upper right")
    return fig


def write_figures(
    out_dir: Path,
    *,
    correctness: dict[str, Any],
    groundness: dict[str, Any],
    png_dpi: int = DEFAULT_PNG_DPI,
) -> list[Path]:
    apply_style()
    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    jobs = (
        (out_dir / "fig1_closeness", fig1_closeness),
        (out_dir / "fig2_confusion", fig2_confusion),
        (out_dir / "fig3_strictness", fig3_strictness),
    )
    for stem, builder in jobs:
        fig = builder(correctness, groundness)
        written.extend(_save(fig, stem, png_dpi))
    return written
