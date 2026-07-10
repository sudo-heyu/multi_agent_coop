#!/usr/bin/env python3
"""Organize tool-memory experiment outputs into per-condition data and stats.

The script is intentionally independent from pandas/scipy so the result package
can be regenerated in the current project environment.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import shutil
from collections import defaultdict
from pathlib import Path
from statistics import mean, stdev
from typing import Iterable

import numpy as np


PROFILE_ORDER = ("none", "basic", "full", "faulty")
MEMORY_ORDER = ("off", "on")
LOWER_IS_BETTER = {"wall_duration_s", "turns"}
HIGHER_IS_BETTER = {"effect_score", "success_rate"}
METRICS = (
    ("wall_duration_s", "Average wall-clock duration", "seconds"),
    ("effect_score", "Average QoS effect score", "score"),
    ("success_rate", "Negotiation success rate", "rate"),
    ("turns", "Average negotiation turns", "turns"),
)
BOOTSTRAP_ROUNDS = 20000
BOOTSTRAP_SEED = 20260710


def _read_rows(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def _write_rows(path: Path, rows: Iterable[dict[str, object]], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: row.get(key, "") for key in fieldnames})


def _num(row: dict[str, str], key: str) -> float | None:
    value = row.get(key)
    if value is None or value == "" or value == "None":
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _success_value(row: dict[str, str]) -> float:
    return 1.0 if str(row.get("success", "")).lower() == "true" else 0.0


def _metric_values(rows: list[dict[str, str]], metric: str) -> list[float]:
    if metric == "success_rate":
        return [_success_value(row) for row in rows]
    values = [_num(row, metric) for row in rows]
    return [float(value) for value in values if value is not None]


def _sample_stats(values: list[float], rng: np.random.Generator) -> dict[str, float | int | None]:
    n = len(values)
    if n == 0:
        return {
            "n": 0,
            "mean": None,
            "std": None,
            "sem": None,
            "ci95_low": None,
            "ci95_high": None,
            "min": None,
            "max": None,
        }
    arr = np.asarray(values, dtype=float)
    if n == 1:
        ci_low = ci_high = float(arr[0])
        std_value = 0.0
        sem_value = 0.0
    else:
        samples = rng.choice(arr, size=(BOOTSTRAP_ROUNDS, n), replace=True).mean(axis=1)
        ci_low, ci_high = np.percentile(samples, [2.5, 97.5])
        std_value = float(stdev(values))
        sem_value = std_value / math.sqrt(n)
    return {
        "n": n,
        "mean": float(arr.mean()),
        "std": std_value,
        "sem": sem_value,
        "ci95_low": float(ci_low),
        "ci95_high": float(ci_high),
        "min": float(arr.min()),
        "max": float(arr.max()),
    }


def _cohens_d(off_values: list[float], on_values: list[float], metric: str) -> float | None:
    if len(off_values) < 2 or len(on_values) < 2:
        return None
    off_std = stdev(off_values)
    on_std = stdev(on_values)
    pooled = math.sqrt(((len(off_values) - 1) * off_std**2 + (len(on_values) - 1) * on_std**2) / (len(off_values) + len(on_values) - 2))
    if pooled == 0:
        return None
    raw = mean(on_values) - mean(off_values)
    if metric in LOWER_IS_BETTER:
        raw = -raw
    return raw / pooled


def _bootstrap_advantage(
    off_values: list[float],
    on_values: list[float],
    metric: str,
    rng: np.random.Generator,
) -> dict[str, float | None]:
    if not off_values or not on_values:
        return {
            "memory_advantage": None,
            "advantage_ci95_low": None,
            "advantage_ci95_high": None,
            "bootstrap_p_two_sided_approx": None,
            "cohens_d_advantage": None,
        }
    off_arr = np.asarray(off_values, dtype=float)
    on_arr = np.asarray(on_values, dtype=float)
    off_samples = rng.choice(off_arr, size=(BOOTSTRAP_ROUNDS, len(off_arr)), replace=True).mean(axis=1)
    on_samples = rng.choice(on_arr, size=(BOOTSTRAP_ROUNDS, len(on_arr)), replace=True).mean(axis=1)
    if metric in LOWER_IS_BETTER:
        advantages = off_samples - on_samples
        advantage = float(off_arr.mean() - on_arr.mean())
    else:
        advantages = on_samples - off_samples
        advantage = float(on_arr.mean() - off_arr.mean())
    ci_low, ci_high = np.percentile(advantages, [2.5, 97.5])
    p_left = float(np.mean(advantages <= 0.0))
    p_right = float(np.mean(advantages >= 0.0))
    p_two = min(1.0, 2.0 * min(p_left, p_right))
    return {
        "memory_advantage": advantage,
        "advantage_ci95_low": float(ci_low),
        "advantage_ci95_high": float(ci_high),
        "bootstrap_p_two_sided_approx": p_two,
        "cohens_d_advantage": _cohens_d(off_values, on_values, metric),
    }


def _copy_condition_artifacts(source_dir: Path, target_dir: Path, profile: str, memory: str, condition: str) -> None:
    raw_source = source_dir / "raw" / condition
    raw_target = target_dir / "raw"
    if raw_source.exists():
        if raw_target.exists():
            shutil.rmtree(raw_target)
        shutil.copytree(raw_source, raw_target)

    db_source = source_dir / "db" / f"{profile}_memory-{memory}.sqlite3"
    if db_source.exists():
        shutil.copy2(db_source, target_dir / "event_db.sqlite3")


def _condition_path(out_dir: Path, profile: str, memory: str) -> Path:
    return out_dir / "data" / profile / f"memory_{memory}"


def _augment_paths(rows: list[dict[str, str]], out_dir: Path) -> list[dict[str, object]]:
    augmented: list[dict[str, object]] = []
    for row in rows:
        item: dict[str, object] = dict(row)
        condition_dir = _condition_path(out_dir, row.get("profile", ""), row.get("memory", ""))
        stdout = Path(row.get("stdout_path", ""))
        stderr = Path(row.get("stderr_path", ""))
        if stdout.name:
            item["local_stdout_path"] = str(condition_dir / "raw" / stdout.name)
        if stderr.name:
            item["local_stderr_path"] = str(condition_dir / "raw" / stderr.name)
        item["local_event_db"] = str(condition_dir / "event_db.sqlite3")
        augmented.append(item)
    return augmented


def _build_stats(
    rows: list[dict[str, str]],
    valid_rows: list[dict[str, str]],
    rng: np.random.Generator,
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    grouped_all: dict[tuple[str, str], list[dict[str, str]]] = defaultdict(list)
    grouped_valid: dict[tuple[str, str], list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        grouped_all[(row.get("profile", ""), row.get("memory", ""))].append(row)
    for row in valid_rows:
        grouped_valid[(row.get("profile", ""), row.get("memory", ""))].append(row)

    condition_stats: list[dict[str, object]] = []
    for profile in PROFILE_ORDER:
        for memory in MEMORY_ORDER:
            key = (profile, memory)
            items = grouped_valid.get(key, [])
            if not items and key not in grouped_all:
                continue
            record: dict[str, object] = {
                "profile": profile,
                "memory": memory,
                "condition": f"{profile}_memory-{memory}",
                "n_all": len(grouped_all.get(key, [])),
                "n_valid": len(items),
                "success_count": int(sum(_success_value(row) for row in items)),
            }
            for metric, _title, _unit in METRICS:
                stats = _sample_stats(_metric_values(items, metric), rng)
                prefix = metric
                record[f"mean_{prefix}"] = stats["mean"]
                record[f"std_{prefix}"] = stats["std"]
                record[f"sem_{prefix}"] = stats["sem"]
                record[f"ci95_low_{prefix}"] = stats["ci95_low"]
                record[f"ci95_high_{prefix}"] = stats["ci95_high"]
                record[f"min_{prefix}"] = stats["min"]
                record[f"max_{prefix}"] = stats["max"]
            condition_stats.append(record)

    pair_stats: list[dict[str, object]] = []
    for profile in PROFILE_ORDER:
        off = grouped_valid.get((profile, "off"), [])
        on = grouped_valid.get((profile, "on"), [])
        if not off or not on:
            continue
        for metric, title, unit in METRICS:
            off_values = _metric_values(off, metric)
            on_values = _metric_values(on, metric)
            off_mean = mean(off_values) if off_values else None
            on_mean = mean(on_values) if on_values else None
            diff = (on_mean - off_mean) if off_mean is not None and on_mean is not None else None
            advantage_stats = _bootstrap_advantage(off_values, on_values, metric, rng)
            pair_stats.append({
                "profile": profile,
                "metric": metric,
                "metric_label": title,
                "unit": unit,
                "desired_direction": "lower" if metric in LOWER_IS_BETTER else "higher",
                "off_n": len(off_values),
                "on_n": len(on_values),
                "off_mean": off_mean,
                "on_mean": on_mean,
                "diff_on_minus_off": diff,
                **advantage_stats,
            })
    return condition_stats, pair_stats


def _fmt_float(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return ""
        return f"{value:.6f}"
    return str(value)


def _write_csv(path: Path, rows: list[dict[str, object]], fieldnames: list[str]) -> None:
    formatted = [{key: _fmt_float(row.get(key)) for key in fieldnames} for row in rows]
    _write_rows(path, formatted, fieldnames)


def _plot_metric(
    condition_stats: list[dict[str, object]],
    metric: str,
    title: str,
    ylabel: str,
    path: Path,
) -> None:
    import matplotlib.pyplot as plt

    profiles = [profile for profile in PROFILE_ORDER if any(row["profile"] == profile for row in condition_stats)]
    x = np.arange(len(profiles))
    width = 0.36
    fig, ax = plt.subplots(figsize=(9.5, 5.2))
    colors = {"off": "#9aa0a6", "on": "#2563eb"}
    for idx, memory in enumerate(MEMORY_ORDER):
        means = []
        lows = []
        highs = []
        for profile in profiles:
            row = next((item for item in condition_stats if item["profile"] == profile and item["memory"] == memory), None)
            mean_value = row.get(f"mean_{metric}") if row else None
            low_value = row.get(f"ci95_low_{metric}") if row else None
            high_value = row.get(f"ci95_high_{metric}") if row else None
            means.append(float(mean_value) if mean_value is not None else 0.0)
            lows.append(float(mean_value) - float(low_value) if mean_value is not None and low_value is not None else 0.0)
            highs.append(float(high_value) - float(mean_value) if mean_value is not None and high_value is not None else 0.0)
        offset = -width / 2 if memory == "off" else width / 2
        ax.bar(
            x + offset,
            means,
            width,
            yerr=[lows, highs],
            capsize=4,
            label=f"memory {memory}",
            color=colors[memory],
            alpha=0.88,
            edgecolor="#202124",
            linewidth=0.6,
        )
    ax.set_title(title)
    ax.set_ylabel(ylabel)
    ax.set_xticks(x)
    ax.set_xticklabels(profiles)
    ax.grid(axis="y", alpha=0.22)
    ax.legend(frameon=False)
    if metric == "success_rate":
        ax.set_ylim(0, 1.05)
    fig.tight_layout()
    fig.savefig(path, dpi=200)
    plt.close(fig)


def _plot_dashboard(condition_stats: list[dict[str, object]], figures_dir: Path) -> None:
    import matplotlib.pyplot as plt

    metrics = METRICS[:3]
    labels = []
    memories = []
    for profile in PROFILE_ORDER:
        for memory in MEMORY_ORDER:
            if any(row["profile"] == profile and row["memory"] == memory for row in condition_stats):
                labels.append(f"{profile}\n{memory}")
                memories.append(memory)
    colors = ["#9aa0a6" if memory == "off" else "#2563eb" for memory in memories]
    fig, axes = plt.subplots(1, 3, figsize=(17, 5.2))
    for ax, (metric, title, unit) in zip(axes, metrics):
        means = []
        yerr_low = []
        yerr_high = []
        for profile in PROFILE_ORDER:
            for memory in MEMORY_ORDER:
                row = next((item for item in condition_stats if item["profile"] == profile and item["memory"] == memory), None)
                if row is None:
                    continue
                mean_value = float(row[f"mean_{metric}"])
                low_value = float(row[f"ci95_low_{metric}"])
                high_value = float(row[f"ci95_high_{metric}"])
                means.append(mean_value)
                yerr_low.append(mean_value - low_value)
                yerr_high.append(high_value - mean_value)
        ax.bar(np.arange(len(labels)), means, yerr=[yerr_low, yerr_high], capsize=3, color=colors, edgecolor="#202124", linewidth=0.5)
        ax.set_title(title)
        ax.set_ylabel(unit)
        ax.set_xticks(np.arange(len(labels)))
        ax.set_xticklabels(labels, fontsize=9)
        ax.grid(axis="y", alpha=0.22)
        if metric == "success_rate":
            ax.set_ylim(0, 1.05)
    handles = [
        plt.Rectangle((0, 0), 1, 1, color="#9aa0a6", label="memory off"),
        plt.Rectangle((0, 0), 1, 1, color="#2563eb", label="memory on"),
    ]
    fig.legend(handles=handles, loc="lower center", bbox_to_anchor=(0.5, 0.0), ncol=2, frameon=False)
    fig.suptitle("8-condition statistical comparison (mean with bootstrap 95% CI)", y=0.99)
    fig.tight_layout(rect=(0, 0.08, 1, 0.94))
    fig.savefig(figures_dir / "00_dashboard_8_conditions_3_metrics.png", dpi=200, bbox_inches="tight")
    plt.close(fig)


def _plot_memory_advantage(pair_stats: list[dict[str, object]], figures_dir: Path) -> None:
    import matplotlib.pyplot as plt

    metrics = [
        ("wall_duration_s", "Duration reduction", "seconds"),
        ("effect_score", "QoS effect gain", "score"),
        ("success_rate", "Success-rate gain", "rate"),
        ("turns", "Turn reduction", "turns"),
    ]
    profiles = [profile for profile in PROFILE_ORDER if any(row["profile"] == profile for row in pair_stats)]
    x = np.arange(len(profiles))
    fig, axes = plt.subplots(2, 2, figsize=(13, 8.5))
    for ax, (metric, title, ylabel) in zip(axes.ravel(), metrics):
        values = []
        lows = []
        highs = []
        for profile in profiles:
            row = next((item for item in pair_stats if item["profile"] == profile and item["metric"] == metric), None)
            if row is None or row.get("memory_advantage") is None:
                values.append(0.0)
                lows.append(0.0)
                highs.append(0.0)
                continue
            value = float(row["memory_advantage"])
            low = float(row["advantage_ci95_low"])
            high = float(row["advantage_ci95_high"])
            values.append(value)
            lows.append(value - low)
            highs.append(high - value)
        ax.bar(x, values, yerr=[lows, highs], capsize=4, color="#16a34a", alpha=0.86, edgecolor="#202124", linewidth=0.6)
        ax.axhline(0, color="#202124", linewidth=0.8)
        ax.set_title(title)
        ax.set_ylabel(ylabel)
        ax.set_xticks(x)
        ax.set_xticklabels(profiles)
        ax.grid(axis="y", alpha=0.22)
    fig.suptitle("Memory advantage by tool profile (positive is better for memory-on)", y=1.01)
    fig.tight_layout()
    fig.savefig(figures_dir / "05_memory_advantage_by_profile.png", dpi=200, bbox_inches="tight")
    plt.close(fig)


def _plot_all(condition_stats: list[dict[str, object]], pair_stats: list[dict[str, object]], figures_dir: Path) -> None:
    import matplotlib

    matplotlib.use("Agg")
    figures_dir.mkdir(parents=True, exist_ok=True)
    _plot_dashboard(condition_stats, figures_dir)
    _plot_metric(
        condition_stats,
        "wall_duration_s",
        "Average negotiation wall-clock duration",
        "seconds",
        figures_dir / "01_avg_wall_duration_s_by_condition.png",
    )
    _plot_metric(
        condition_stats,
        "effect_score",
        "Average QoS effect score",
        "score",
        figures_dir / "02_avg_qos_effect_score_by_condition.png",
    )
    _plot_metric(
        condition_stats,
        "success_rate",
        "Average negotiation success rate",
        "rate",
        figures_dir / "03_success_rate_by_condition.png",
    )
    _plot_metric(
        condition_stats,
        "turns",
        "Average negotiation turns",
        "turns",
        figures_dir / "04_avg_turns_by_condition.png",
    )
    _plot_memory_advantage(pair_stats, figures_dir)


def _write_readme(out_dir: Path, source_dir: Path, valid_rows: list[dict[str, str]], condition_stats: list[dict[str, object]]) -> None:
    lines = [
        "# Tool-memory experiment structured results",
        "",
        f"Source directory: `{source_dir}`",
        f"Valid completed runs used for statistics: {len(valid_rows)}",
        "",
        "Directory layout:",
        "- `data/<profile>/memory_<off|on>/`: per-condition CSV rows, raw stdout/stderr logs, and SQLite event DB.",
        "- `stats/`: statistical tables with mean/std/SEM/bootstrap 95% CI and memory-on/off comparisons.",
        "- `figures/`: all comparison figures. The dashboard compares the 8 profile-memory conditions across the three primary metrics.",
        "",
        "Primary metrics:",
        "- `wall_duration_s`: wall-clock negotiation duration; lower is better.",
        "- `effect_score`: QoS acceptance/effect score; higher is better.",
        "- `success_rate`: fraction of completed runs whose negotiation outcome was success; higher is better.",
        "",
        "Condition summary:",
        "",
        "| profile | memory | n_valid | duration mean | effect mean | success rate |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for row in condition_stats:
        lines.append(
            "| {profile} | {memory} | {n_valid} | {duration:.2f} | {effect:.3f} | {success:.2f} |".format(
                profile=row["profile"],
                memory=row["memory"],
                n_valid=row["n_valid"],
                duration=float(row["mean_wall_duration_s"]),
                effect=float(row["mean_effect_score"]),
                success=float(row["mean_success_rate"]),
            )
        )
    (out_dir / "README.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def organize(source_dir: Path, out_dir: Path, *, force: bool) -> None:
    source_dir = source_dir.resolve()
    out_dir = out_dir.resolve()
    results_csv = source_dir / "results.csv"
    if not results_csv.exists():
        raise FileNotFoundError(f"missing results.csv: {results_csv}")
    if out_dir.exists():
        if not force:
            raise FileExistsError(f"output directory exists; pass --force to replace it: {out_dir}")
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True)

    rows = _read_rows(results_csv)
    valid_rows = [row for row in rows if row.get("run_status", "completed") == "completed"]
    augmented_all = _augment_paths(rows, out_dir)
    augmented_valid = _augment_paths(valid_rows, out_dir)
    fieldnames = list(rows[0].keys()) if rows else []
    for extra in ("local_stdout_path", "local_stderr_path", "local_event_db"):
        if extra not in fieldnames:
            fieldnames.append(extra)
    _write_rows(out_dir / "all_results.csv", augmented_all, fieldnames)
    _write_rows(out_dir / "valid_results.csv", augmented_valid, fieldnames)

    grouped_all: dict[tuple[str, str], list[dict[str, str]]] = defaultdict(list)
    grouped_valid: dict[tuple[str, str], list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        grouped_all[(row.get("profile", ""), row.get("memory", ""))].append(row)
    for row in valid_rows:
        grouped_valid[(row.get("profile", ""), row.get("memory", ""))].append(row)

    for profile in PROFILE_ORDER:
        for memory in MEMORY_ORDER:
            condition = f"{profile}_memory-{memory}"
            condition_dir = _condition_path(out_dir, profile, memory)
            condition_dir.mkdir(parents=True, exist_ok=True)
            all_items = grouped_all.get((profile, memory), [])
            valid_items = grouped_valid.get((profile, memory), [])
            _write_rows(condition_dir / "all_rows.csv", _augment_paths(all_items, out_dir), fieldnames)
            _write_rows(condition_dir / "valid_rows.csv", _augment_paths(valid_items, out_dir), fieldnames)
            _copy_condition_artifacts(source_dir, condition_dir, profile, memory, condition)

    rng = np.random.default_rng(BOOTSTRAP_SEED)
    condition_stats, pair_stats = _build_stats(rows, valid_rows, rng)
    stats_dir = out_dir / "stats"
    stats_dir.mkdir(parents=True, exist_ok=True)

    condition_fields = [
        "profile",
        "memory",
        "condition",
        "n_all",
        "n_valid",
        "success_count",
    ]
    for metric, _title, _unit in METRICS:
        condition_fields.extend([
            f"mean_{metric}",
            f"std_{metric}",
            f"sem_{metric}",
            f"ci95_low_{metric}",
            f"ci95_high_{metric}",
            f"min_{metric}",
            f"max_{metric}",
        ])
    pair_fields = [
        "profile",
        "metric",
        "metric_label",
        "unit",
        "desired_direction",
        "off_n",
        "on_n",
        "off_mean",
        "on_mean",
        "diff_on_minus_off",
        "memory_advantage",
        "advantage_ci95_low",
        "advantage_ci95_high",
        "bootstrap_p_two_sided_approx",
        "cohens_d_advantage",
    ]
    _write_csv(stats_dir / "condition_statistics.csv", condition_stats, condition_fields)
    _write_csv(stats_dir / "memory_pair_statistics.csv", pair_stats, pair_fields)
    (stats_dir / "metric_definitions.json").write_text(
        json.dumps(
            {
                "bootstrap_rounds": BOOTSTRAP_ROUNDS,
                "bootstrap_seed": BOOTSTRAP_SEED,
                "valid_row_filter": "run_status == completed",
                "memory_advantage": {
                    "wall_duration_s": "off_mean - on_mean",
                    "turns": "off_mean - on_mean",
                    "effect_score": "on_mean - off_mean",
                    "success_rate": "on_mean - off_mean",
                },
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    if (source_dir / "config.json").exists():
        shutil.copy2(source_dir / "config.json", out_dir / "config.json")
    if (source_dir / "summary.json").exists():
        shutil.copy2(source_dir / "summary.json", out_dir / "source_summary.json")

    _plot_all(condition_stats, pair_stats, out_dir / "figures")
    _write_readme(out_dir, source_dir, valid_rows, condition_stats)


def main() -> None:
    parser = argparse.ArgumentParser(description="Organize tool-memory experiment results")
    parser.add_argument(
        "--source-dir",
        default="logs/experiments/tool_memory_results_final_20260710",
        help="Directory containing results.csv, raw/, and db/.",
    )
    parser.add_argument(
        "--out-dir",
        default="logs/experiments/tool_memory_results_structured_20260710",
        help="Structured output directory to create.",
    )
    parser.add_argument("--force", action="store_true", help="Replace the output directory if it already exists.")
    args = parser.parse_args()
    organize(Path(args.source_dir), Path(args.out_dir), force=args.force)


if __name__ == "__main__":
    main()
