#!/usr/bin/env python3
"""Create the single final figure for the tool-memory experiment."""
from __future__ import annotations

import argparse
import csv
import shutil
from pathlib import Path

import numpy as np


PROFILE_ORDER = ("none", "basic", "full", "faulty")
MEMORY_ORDER = ("off", "on")
MEMORY_COLORS = {"off": "#9aa0a6", "on": "#2563eb"}
ADVANTAGE_COLOR = "#16a34a"


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def _f(row: dict[str, str], key: str) -> float:
    value = row.get(key, "")
    return float(value) if value not in {"", "None"} else 0.0


def _condition_row(rows: list[dict[str, str]], profile: str, memory: str) -> dict[str, str]:
    for row in rows:
        if row["profile"] == profile and row["memory"] == memory:
            return row
    raise KeyError(f"missing condition stats for {profile}/memory-{memory}")


def _pair_row(rows: list[dict[str, str]], profile: str, metric: str) -> dict[str, str]:
    for row in rows:
        if row["profile"] == profile and row["metric"] == metric:
            return row
    raise KeyError(f"missing pair stats for {profile}/{metric}")


def _plot_condition_metric(ax, rows: list[dict[str, str]], *, metric: str, title: str, ylabel: str, ylim: tuple[float, float] | None = None) -> None:
    x = np.arange(len(PROFILE_ORDER))
    width = 0.36
    for memory in MEMORY_ORDER:
        offset = -width / 2 if memory == "off" else width / 2
        means: list[float] = []
        lows: list[float] = []
        highs: list[float] = []
        for profile in PROFILE_ORDER:
            row = _condition_row(rows, profile, memory)
            mean = _f(row, f"mean_{metric}")
            low = _f(row, f"ci95_low_{metric}")
            high = _f(row, f"ci95_high_{metric}")
            means.append(mean)
            lows.append(mean - low)
            highs.append(high - mean)
        ax.bar(
            x + offset,
            means,
            width,
            yerr=[lows, highs],
            capsize=3.5,
            label=f"memory {memory}",
            color=MEMORY_COLORS[memory],
            alpha=0.9,
            edgecolor="#202124",
            linewidth=0.55,
        )
    ax.set_title(title, fontsize=12, fontweight="bold")
    ax.set_ylabel(ylabel)
    ax.set_xticks(x)
    ax.set_xticklabels(PROFILE_ORDER)
    ax.grid(axis="y", alpha=0.22)
    if ylim:
        ax.set_ylim(*ylim)


def _plot_advantage_metric(ax, rows: list[dict[str, str]], *, metric: str, title: str, ylabel: str) -> None:
    x = np.arange(len(PROFILE_ORDER))
    values: list[float] = []
    lows: list[float] = []
    highs: list[float] = []
    for profile in PROFILE_ORDER:
        row = _pair_row(rows, profile, metric)
        value = _f(row, "memory_advantage")
        low = _f(row, "advantage_ci95_low")
        high = _f(row, "advantage_ci95_high")
        values.append(value)
        lows.append(value - low)
        highs.append(high - value)
    ax.bar(
        x,
        values,
        yerr=[lows, highs],
        capsize=3.5,
        color=ADVANTAGE_COLOR,
        alpha=0.9,
        edgecolor="#202124",
        linewidth=0.55,
    )
    ax.axhline(0, color="#202124", linewidth=0.8)
    ax.set_title(title, fontsize=12, fontweight="bold")
    ax.set_ylabel(ylabel)
    ax.set_xticks(x)
    ax.set_xticklabels(PROFILE_ORDER)
    ax.grid(axis="y", alpha=0.22)


def make_figure(stats_dir: Path, out_dir: Path, *, force: bool) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    condition_rows = _read_csv(stats_dir / "condition_statistics.csv")
    pair_rows = _read_csv(stats_dir / "memory_pair_statistics.csv")

    if out_dir.exists():
        if not force:
            raise FileExistsError(f"output directory exists; pass --force to replace it: {out_dir}")
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    plt.rcParams.update({
        "font.size": 10,
        "axes.titlesize": 12,
        "axes.labelsize": 10,
        "legend.fontsize": 10,
        "figure.titlesize": 16,
    })

    fig, axes = plt.subplots(2, 3, figsize=(16.5, 9.4))
    _plot_condition_metric(
        axes[0, 0],
        condition_rows,
        metric="wall_duration_s",
        title="A. Mean negotiation duration",
        ylabel="seconds (lower is better)",
    )
    _plot_condition_metric(
        axes[0, 1],
        condition_rows,
        metric="effect_score",
        title="B. Mean QoS effect score",
        ylabel="score (higher is better)",
    )
    _plot_condition_metric(
        axes[0, 2],
        condition_rows,
        metric="success_rate",
        title="C. Mean negotiation success rate",
        ylabel="rate (higher is better)",
        ylim=(0, 1.08),
    )
    _plot_advantage_metric(
        axes[1, 0],
        pair_rows,
        metric="wall_duration_s",
        title="D. Memory advantage: duration reduction",
        ylabel="off - on seconds",
    )
    _plot_advantage_metric(
        axes[1, 1],
        pair_rows,
        metric="effect_score",
        title="E. Memory advantage: QoS effect gain",
        ylabel="on - off score",
    )
    _plot_advantage_metric(
        axes[1, 2],
        pair_rows,
        metric="success_rate",
        title="F. Memory advantage: success-rate gain",
        ylabel="on - off rate",
    )

    handles = [
        plt.Rectangle((0, 0), 1, 1, color=MEMORY_COLORS["off"], label="memory off"),
        plt.Rectangle((0, 0), 1, 1, color=MEMORY_COLORS["on"], label="memory on"),
        plt.Rectangle((0, 0), 1, 1, color=ADVANTAGE_COLOR, label="memory advantage"),
    ]
    fig.legend(handles=handles, loc="lower center", ncol=3, frameon=False, bbox_to_anchor=(0.5, 0.01))
    fig.suptitle(
        "Tool-profile x memory experiment: final statistical outcome (n=10 per condition, bootstrap 95% CI)",
        y=0.98,
        fontweight="bold",
    )
    fig.text(
        0.5,
        0.945,
        "Positive bars in D-F favor memory-on. Tool profiles: no tools, basic tools, full tools, faulty tools.",
        ha="center",
        fontsize=10,
        color="#3c4043",
    )
    fig.tight_layout(rect=(0.02, 0.065, 0.995, 0.925))

    png_path = out_dir / "ultimate_tool_memory_experiment_result.png"
    pdf_path = out_dir / "ultimate_tool_memory_experiment_result.pdf"
    fig.savefig(png_path, dpi=240)
    fig.savefig(pdf_path)
    plt.close(fig)


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate the single final result figure for tool-memory experiments")
    parser.add_argument(
        "--stats-dir",
        default="logs/experiments/tool_memory_results_structured_20260710/stats",
        help="Directory containing condition_statistics.csv and memory_pair_statistics.csv.",
    )
    parser.add_argument(
        "--out-dir",
        default="logs/experiments/tool_memory_final_figures_20260710",
        help="Clean directory that will contain only the final result figures.",
    )
    parser.add_argument("--force", action="store_true", help="Replace the output directory if it exists.")
    args = parser.parse_args()
    make_figure(Path(args.stats_dir), Path(args.out_dir), force=args.force)


if __name__ == "__main__":
    main()
