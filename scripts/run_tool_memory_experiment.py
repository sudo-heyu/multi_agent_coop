#!/usr/bin/env python3
"""Run and summarize the tool-profile x memory experiment matrix.

Default matrix:
  tool profiles: none, basic, full, faulty
  memory modes: off, on
  repetitions: 10

Each condition gets its own SQLite event DB so memory-on runs accumulate only
within the same condition, while memory-off runs remain cleanly disabled by
MULTIAP_MEMORY_MODE=off.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import shutil
import subprocess
import sys
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from statistics import mean

import requests


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.tools.edca import encode_params_edca  # noqa: E402

DEFAULT_PROFILES = ("none", "basic", "full", "faulty")
DEFAULT_MEMORIES = ("off", "on")
AP_IDS = ("ap1", "ap2", "ap3")
BASELINE_SR_PARAMS = {"tx_power_dbm": 10, "obss_pd_dbm": -82}
BASELINE_EDCA_PARAMS = {
    "ap1": {
        "CWmin": 31, "CWmax": 63, "AIFSN": 7,
        "VI_CWmin": 31, "VI_CWmax": 63, "VI_AIFSN": 7,
    },
    "ap2": {
        "CWmin": 7, "CWmax": 15, "AIFSN": 2,
        "VI_CWmin": 7, "VI_CWmax": 15, "VI_AIFSN": 2,
    },
    "ap3": {
        "CWmin": 15, "CWmax": 31, "AIFSN": 3,
        "VI_CWmin": 15, "VI_CWmax": 31, "VI_AIFSN": 3,
    },
}
EDCA_OBS_KEYS = {
    "CWmin": ("cwmin", "be_cwmin"),
    "CWmax": ("cwmax", "be_cwmax"),
    "AIFSN": ("aifsn", "be_aifsn"),
    "VI_CWmin": ("vi_cwmin",),
    "VI_CWmax": ("vi_cwmax",),
    "VI_AIFSN": ("vi_aifsn",),
}


SESSION_RE = re.compile(r"\[Run\]\s+session_id=([0-9a-fA-F-]+)")
OUTCOME_RE = re.compile(r"outcome=.*?([A-Za-z_]+).*?turns=(\d+).*?用时\s+([0-9.]+)s")
STRATEGY_RE = re.compile(r"\[策略\]\s*(\S+)")
QOS_RE = re.compile(
    r"(?:\[QoS\]|\bQoS\b).*?verdict=([A-Za-z_]+)\s+"
    r"score=([-+0-9.eE]+|None)\s+confidence=([-+0-9.eE]+|None)"
)
ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
INFRA_ERROR_MARKERS = (
    "Read timed out",
    "ReadTimeoutError",
    "ChunkedEncodingError",
    "ProtocolError",
    "Response ended prematurely",
    "ConnectionError",
    "HTTPSConnectionPool",
    "RemoteDisconnected",
    "Connection reset",
    "Max retries exceeded",
    "SSLError",
    "SSLEOFError",
    "UNEXPECTED_EOF",
    "EOF occurred",
    "ProxyError",
    "PPIO stream API HTTP 5",
    "PPIO stream API HTTP 429",
    "server overload",
)


def _float_or_none(value: str | None) -> float | None:
    if value is None or value == "" or value == "None":
        return None
    try:
        return float(value)
    except ValueError:
        return None


def parse_run_output(text: str) -> dict[str, object]:
    text = ANSI_RE.sub("", text)
    session = SESSION_RE.search(text)
    outcome = OUTCOME_RE.search(text)
    strategy = STRATEGY_RE.search(text)
    qos = QOS_RE.search(text)
    data: dict[str, object] = {
        "session_id": session.group(1) if session else "",
        "outcome": outcome.group(1) if outcome else "",
        "turns": int(outcome.group(2)) if outcome else None,
        "reported_duration_s": _float_or_none(outcome.group(3)) if outcome else None,
        "strategy": strategy.group(1) if strategy else "",
        "qos_verdict": qos.group(1) if qos else "",
        "qos_score": _float_or_none(qos.group(2)) if qos else None,
        "qos_confidence": _float_or_none(qos.group(3)) if qos else None,
    }
    # Penalized score for plots/statistics: failed runs with no QoS observation
    # should not look equivalent to neutral zero.
    score = data["qos_score"]
    data["effect_score"] = score if isinstance(score, float) else -1.0
    data["success"] = data["outcome"] == "success"
    return data


def _decode_timeout_output(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def classify_run_status(returncode: object, stdout: str, stderr: str) -> str:
    """Classify whether a subprocess produced usable experiment data.

    Completed negotiations include both success and domain-level failures such
    as max_retries_exceeded; infrastructure failures are retried and excluded
    from aggregate statistics unless all retry attempts fail.
    """
    if returncode in (0, "0"):
        return "completed"
    if returncode == "timeout":
        return "infra_timeout"
    text = f"{stdout}\n{stderr}"
    if any(marker in text for marker in INFRA_ERROR_MARKERS):
        return "infra_error"
    return "run_error"


def condition_db(out_dir: Path, profile: str, memory: str) -> Path:
    return out_dir / "db" / f"{profile}_memory-{memory}.sqlite3"


def seed_memory_db(args, out_dir: Path, profile: str, memory: str) -> None:
    if memory != "on" or not args.seed_memory_db:
        return
    source = Path(args.seed_memory_db)
    if not source.is_absolute():
        source = REPO_ROOT / source
    if not source.exists():
        raise RuntimeError(f"seed memory DB not found: {source}")
    target = condition_db(out_dir, profile, memory)
    if target.exists() and not args.reseed_memory:
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)


def load_ap_endpoints(path: str) -> dict[str, str]:
    if not path:
        return {}
    config_path = Path(path)
    if not config_path.is_absolute():
        config_path = REPO_ROOT / config_path
    data = json.loads(config_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        return {}
    return {
        str(ap_id).lower(): str(url)
        for ap_id, url in data.items()
        if str(ap_id).lower() in {"ap1", "ap2", "ap3"} and str(url).strip()
    }


def _state_payload(server: str, *, timeout: float) -> dict[str, object]:
    resp = requests.get(f"{server.rstrip('/')}/state", timeout=timeout)
    resp.raise_for_status()
    data = resp.json()
    return data if isinstance(data, dict) else {}


def _baseline_unstable_reason(payload: dict[str, object], *, max_age: float) -> str | None:
    for ap_id in AP_IDS:
        entry = payload.get(ap_id)
        if not isinstance(entry, dict):
            return f"{ap_id} missing"
        if entry.get("stale"):
            return f"{ap_id} stale"
        age = _float_or_none(str(entry.get("age_seconds")))
        if age is None or age > max_age:
            return f"{ap_id} age={entry.get('age_seconds')}"
        state = entry.get("data")
        if not isinstance(state, dict):
            return f"{ap_id} missing data"
        if _float_or_none(str(state.get("tx_power_dbm"))) != BASELINE_SR_PARAMS["tx_power_dbm"]:
            return f"{ap_id} tx_power={state.get('tx_power_dbm')}"
        if _float_or_none(str(state.get("obss_pd_dbm"))) != BASELINE_SR_PARAMS["obss_pd_dbm"]:
            return f"{ap_id} obss_pd={state.get('obss_pd_dbm')}"
        expected_edca = encode_params_edca(BASELINE_EDCA_PARAMS[ap_id])
        for param_key, state_keys in EDCA_OBS_KEYS.items():
            expected = _float_or_none(str(expected_edca.get(param_key)))
            if expected is None:
                continue
            for state_key in state_keys:
                actual = _float_or_none(str(state.get(state_key)))
                if actual != expected:
                    return f"{ap_id} {state_key}={state.get(state_key)} expected={expected}"
        throughput = _float_or_none(str(state.get("throughput_mbps_user")))
        if throughput is None:
            return f"{ap_id} throughput={state.get('throughput_mbps_user')}"
    return None


def wait_for_stable_baseline(args) -> None:
    if args.dry_run or args.mode != "ns3" or args.no_wait_stable_baseline:
        return
    deadline = time.time() + max(0.0, args.reset_stable_timeout)
    last_reason = "not checked"
    while True:
        try:
            payload = _state_payload(args.server, timeout=args.reset_state_timeout)
            last_reason = _baseline_unstable_reason(
                payload,
                max_age=max(1.0, args.reset_state_max_age),
            ) or ""
            if not last_reason:
                return
        except (requests.RequestException, ValueError) as exc:
            last_reason = str(exc)
        if time.time() >= deadline:
            raise RuntimeError(f"baseline did not stabilize: {last_reason}")
        time.sleep(max(0.1, args.reset_poll_interval))


def reset_ns3_baseline(args, *, session_id: str) -> None:
    if args.dry_run or args.mode != "ns3" or args.no_reset_baseline:
        return
    endpoints = load_ap_endpoints(args.ap_config)
    if not endpoints:
        return
    errors: list[str] = []
    for ap_id, url in sorted(endpoints.items()):
        payloads = [
            {
                "session_id": session_id,
                "strategy": "co_sr",
                "ap_id": ap_id,
                "params": dict(BASELINE_SR_PARAMS),
            },
            {
                "session_id": session_id,
                "strategy": "co_edca",
                "ap_id": ap_id,
                "params": encode_params_edca(BASELINE_EDCA_PARAMS[ap_id]),
            },
        ]
        for payload in payloads:
            last_error = ""
            for attempt in range(1, max(1, args.reset_attempts) + 1):
                try:
                    resp = requests.post(
                        f"{url.rstrip('/')}/apply",
                        json=payload,
                        timeout=args.reset_timeout,
                    )
                except requests.RequestException as exc:
                    last_error = str(exc)
                else:
                    if resp.status_code == 200:
                        last_error = ""
                        break
                    last_error = f"HTTP {resp.status_code} {resp.text[:300]}"
                if attempt < max(1, args.reset_attempts):
                    time.sleep(max(0.0, args.reset_retry_delay))
            if last_error:
                errors.append(f"{ap_id} {payload['strategy']}: {last_error}")
    if errors:
        raise RuntimeError("baseline reset failed: " + " | ".join(errors))
    if args.reset_settle > 0:
        time.sleep(args.reset_settle)
    wait_for_stable_baseline(args)


def reset_failure_row(
    args,
    profile: str,
    memory: str,
    trial: int,
    out_dir: Path,
    *,
    attempt: int,
    error: Exception,
) -> dict[str, object]:
    condition = f"{profile}_memory-{memory}"
    suffix = f"trial_{trial:02d}" if attempt == 1 else f"trial_{trial:02d}_attempt_{attempt:02d}"
    stderr_path = out_dir / "raw" / condition / f"{suffix}.stderr.txt"
    stdout_path = out_dir / "raw" / condition / f"{suffix}.stdout.txt"
    stdout_path.parent.mkdir(parents=True, exist_ok=True)
    stdout_path.write_text("", encoding="utf-8")
    stderr_path.write_text(f"[experiment] baseline reset failed: {error}\n", encoding="utf-8")
    return {
        "profile": profile,
        "memory": memory,
        "condition": condition,
        "trial": trial,
        "attempt": attempt,
        "returncode": "reset_failed",
        "run_status": "infra_error",
        "outcome": "baseline_reset_failed",
        "success": False,
        "effect_score": -1.0,
        "event_db": str(condition_db(out_dir, profile, memory)),
        "stdout_path": str(stdout_path),
        "stderr_path": str(stderr_path),
        "command": "baseline reset",
    }


def run_one(
    args,
    profile: str,
    memory: str,
    trial: int,
    out_dir: Path,
    *,
    attempt: int = 1,
) -> dict[str, object]:
    condition = f"{profile}_memory-{memory}"
    suffix = f"trial_{trial:02d}" if attempt == 1 else f"trial_{trial:02d}_attempt_{attempt:02d}"
    stdout_path = out_dir / "raw" / condition / f"{suffix}.stdout.txt"
    stderr_path = out_dir / "raw" / condition / f"{suffix}.stderr.txt"
    stdout_path.parent.mkdir(parents=True, exist_ok=True)
    stderr_path.parent.mkdir(parents=True, exist_ok=True)
    db_path = condition_db(out_dir, profile, memory)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    cmd = [
        sys.executable,
        "run.py",
        "--mode", args.mode,
        "--scene", args.scene,
        "--server", args.server,
        "--tool-profile", profile,
        "--memory", memory,
        "--acceptance", args.acceptance,
        "--no-dashboard",
        "--no-academic-plot",
        "--eval-windows", args.eval_windows,
        "--max-steps", str(args.max_steps),
        "--max-validation-retries", str(args.max_validation_retries),
        "--state-wait", str(args.state_wait),
        "--observation-wait", str(args.observation_wait),
    ]
    if args.ap_config:
        cmd += ["--ap-config", args.ap_config]
    if args.model:
        cmd += ["--model", args.model]
    if args.base_url:
        cmd += ["--base-url", args.base_url]
    if args.stream_timeout is not None:
        cmd += ["--stream-timeout", str(args.stream_timeout)]
    if args.max_tool_rounds is not None:
        cmd += ["--max-tool-rounds", str(args.max_tool_rounds)]

    env = dict(os.environ)
    env["MULTIAP_EVENT_DB"] = str(db_path)
    env["MULTIAP_MEMORY_MODE"] = memory
    env["MULTIAP_TOOL_PROFILE"] = profile
    env["MULTIAP_BROADCAST_WORKERS"] = str(args.broadcast_workers)
    env["PYTHONUNBUFFERED"] = "1"
    if args.improve_threshold is not None:
        env["MULTIAP_IMPROVE_THRESHOLD"] = str(args.improve_threshold)
    if args.degrade_threshold is not None:
        env["MULTIAP_DEGRADE_THRESHOLD"] = str(args.degrade_threshold)
    if args.qos_sample_count is not None:
        env["MULTIAP_QOS_SAMPLE_COUNT"] = str(args.qos_sample_count)
    if args.qos_sample_interval is not None:
        env["MULTIAP_QOS_SAMPLE_INTERVAL"] = str(args.qos_sample_interval)

    started = time.time()
    if args.dry_run:
        stdout = "[dry-run]\n" + " ".join(cmd)
        stderr = ""
        returncode = 0
    else:
        stdout_path.parent.mkdir(parents=True, exist_ok=True)
        stderr_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with stdout_path.open("w", encoding="utf-8") as out_fh, \
                    stderr_path.open("w", encoding="utf-8") as err_fh:
                proc = subprocess.Popen(
                    cmd,
                    cwd=REPO_ROOT,
                    env=env,
                    text=True,
                    stdout=out_fh,
                    stderr=err_fh,
                )
                try:
                    returncode: object = proc.wait(timeout=args.timeout)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
                    err_fh.write(f"\n[experiment] subprocess timeout after {args.timeout}s\n")
                    returncode = "timeout"
        except subprocess.TimeoutExpired as exc:
            stdout = _decode_timeout_output(exc.stdout)
            stderr = _decode_timeout_output(exc.stderr)
            returncode = "timeout"
        else:
            stdout = stdout_path.read_text(encoding="utf-8", errors="replace")
            stderr = stderr_path.read_text(encoding="utf-8", errors="replace")
    wall = time.time() - started

    if args.dry_run:
        stdout_path.write_text(stdout, encoding="utf-8")
        stderr_path.write_text(stderr, encoding="utf-8")

    parsed = parse_run_output(stdout)
    run_status = classify_run_status(returncode, stdout, stderr)
    if run_status == "completed" and parsed.get("outcome") == "qos_apply_failed":
        run_status = "infra_error"
    if run_status != "completed" and not parsed.get("outcome"):
        parsed["outcome"] = run_status
        parsed["success"] = False
        parsed["effect_score"] = -1.0
    parsed.update({
        "profile": profile,
        "memory": memory,
        "condition": condition,
        "trial": trial,
        "attempt": attempt,
        "returncode": returncode,
        "run_status": run_status,
        "wall_duration_s": round(wall, 3),
        "event_db": str(db_path),
        "stdout_path": str(stdout_path),
        "stderr_path": str(stderr_path),
        "command": " ".join(cmd),
    })
    return parsed


def append_csv(path: Path, row: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    exists = path.exists()
    fieldnames = [
        "profile", "memory", "condition", "trial", "session_id",
        "attempt", "attempts", "infra_retries", "returncode", "run_status",
        "outcome", "success", "strategy", "turns",
        "reported_duration_s", "wall_duration_s", "qos_verdict",
        "qos_score", "qos_confidence", "effect_score", "event_db",
        "stdout_path", "stderr_path", "command",
    ]
    with path.open("a", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        if not exists:
            writer.writeheader()
        writer.writerow({key: row.get(key, "") for key in fieldnames})


def read_rows(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def _num(row: dict[str, str], key: str) -> float | None:
    return _float_or_none(row.get(key))


def summarize(csv_path: Path, out_dir: Path) -> dict[str, object]:
    rows = read_rows(csv_path)
    valid_rows = [
        row for row in rows
        if row.get("run_status", "completed") == "completed"
    ]
    grouped: dict[tuple[str, str], list[dict[str, str]]] = defaultdict(list)
    for row in valid_rows:
        grouped[(row["profile"], row["memory"])].append(row)

    conditions = []
    for (profile, memory), items in sorted(grouped.items()):
        turns = [_num(row, "turns") for row in items]
        effects = [_num(row, "effect_score") for row in items]
        durations = [_num(row, "wall_duration_s") for row in items]
        conditions.append({
            "profile": profile,
            "memory": memory,
            "n": len(items),
            "success_rate": mean([row.get("success") == "True" for row in items]),
            "avg_turns": mean([v for v in turns if v is not None]) if any(v is not None for v in turns) else None,
            "avg_effect_score": mean([v for v in effects if v is not None]) if any(v is not None for v in effects) else None,
            "avg_wall_duration_s": mean([v for v in durations if v is not None]) if any(v is not None for v in durations) else None,
        })

    by_profile: dict[str, dict[str, dict[str, object]]] = defaultdict(dict)
    for item in conditions:
        by_profile[str(item["profile"])][str(item["memory"])] = item
    gaps = []
    for profile, pair in sorted(by_profile.items()):
        on = pair.get("on")
        off = pair.get("off")
        if not on or not off:
            continue
        gaps.append({
            "profile": profile,
            "turn_reduction": (
                float(off["avg_turns"]) - float(on["avg_turns"])
                if off.get("avg_turns") is not None and on.get("avg_turns") is not None
                else None
            ),
            "duration_reduction_s": (
                float(off["avg_wall_duration_s"]) - float(on["avg_wall_duration_s"])
                if off.get("avg_wall_duration_s") is not None and on.get("avg_wall_duration_s") is not None
                else None
            ),
            "success_rate_gain": (
                float(on["success_rate"]) - float(off["success_rate"])
                if off.get("success_rate") is not None and on.get("success_rate") is not None
                else None
            ),
            "effect_gain": (
                float(on["avg_effect_score"]) - float(off["avg_effect_score"])
                if off.get("avg_effect_score") is not None and on.get("avg_effect_score") is not None
                else None
            ),
        })
    summary = {
        "conditions": conditions,
        "memory_gaps": gaps,
        "rows": len(rows),
        "valid_rows": len(valid_rows),
        "infra_rows": len(rows) - len(valid_rows),
    }
    (out_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return summary


def plot(csv_path: Path, out_dir: Path) -> None:
    os.environ.setdefault("MPLCONFIGDIR", str(out_dir / ".mplconfig"))
    try:
        import matplotlib.pyplot as plt
    except Exception as exc:  # noqa: BLE001
        (out_dir / "plot_error.txt").write_text(str(exc), encoding="utf-8")
        return

    rows = [
        row for row in read_rows(csv_path)
        if row.get("run_status", "completed") == "completed"
    ]
    profiles = [p for p in DEFAULT_PROFILES if any(row["profile"] == p for row in rows)]
    memories = ["off", "on"]
    grouped: dict[tuple[str, str], list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        grouped[(row["profile"], row["memory"])].append(row)

    def avg(profile: str, memory: str, key: str) -> float:
        values = [_num(row, key) for row in grouped[(profile, memory)]]
        values = [value for value in values if value is not None]
        return mean(values) if values else 0.0

    x = list(range(len(profiles)))
    width = 0.36
    fig, axes = plt.subplots(1, 3, figsize=(16, 4.5))
    for idx, memory in enumerate(memories):
        offset = (-width / 2) if memory == "off" else (width / 2)
        axes[0].bar(
            [v + offset for v in x],
            [avg(profile, memory, "turns") for profile in profiles],
            width,
            label=f"memory {memory}",
        )
        axes[1].bar(
            [v + offset for v in x],
            [avg(profile, memory, "effect_score") for profile in profiles],
            width,
            label=f"memory {memory}",
        )
        axes[2].bar(
            [v + offset for v in x],
            [avg(profile, memory, "wall_duration_s") for profile in profiles],
            width,
            label=f"memory {memory}",
        )
    axes[0].set_title("Average negotiation turns")
    axes[0].set_ylabel("turns")
    axes[1].set_title("Average QoS effect score")
    axes[1].set_ylabel("score")
    axes[2].set_title("Average wall-clock duration")
    axes[2].set_ylabel("seconds")
    for ax in axes:
        ax.set_xticks(x)
        ax.set_xticklabels(profiles)
        ax.grid(axis="y", alpha=0.25)
        ax.legend()
    fig.tight_layout()
    fig.savefig(out_dir / "summary_bars.png", dpi=180)
    plt.close(fig)

    fig, ax = plt.subplots(figsize=(8, 4.5))
    effect_gains = [avg(profile, "on", "effect_score") - avg(profile, "off", "effect_score") for profile in profiles]
    turn_reductions = [avg(profile, "off", "turns") - avg(profile, "on", "turns") for profile in profiles]
    duration_reductions = [
        avg(profile, "off", "wall_duration_s") - avg(profile, "on", "wall_duration_s")
        for profile in profiles
    ]
    ax.plot(profiles, effect_gains, marker="o", label="effect gain (on - off)")
    ax.plot(profiles, turn_reductions, marker="s", label="turn reduction (off - on)")
    ax.plot(profiles, duration_reductions, marker="^", label="duration reduction sec (off - on)")
    ax.axhline(0, color="black", linewidth=0.8)
    ax.set_title("Memory advantage by tool profile")
    ax.grid(axis="y", alpha=0.25)
    ax.legend()
    fig.tight_layout()
    fig.savefig(out_dir / "memory_advantage.png", dpi=180)
    plt.close(fig)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run 4x2 Multi-AP memory/tool experiments")
    parser.add_argument("--mode", choices=["ns3", "real"], default="ns3")
    parser.add_argument("--scene", default="sr")
    parser.add_argument("--server", default="http://127.0.0.1:5001")
    parser.add_argument("--ap-config", default="config/ap_endpoints.json")
    parser.add_argument("--acceptance", choices=["validator", "qos"], default="qos")
    parser.add_argument("--profiles", default=",".join(DEFAULT_PROFILES))
    parser.add_argument("--memories", default=",".join(DEFAULT_MEMORIES))
    parser.add_argument("--repetitions", type=int, default=10)
    parser.add_argument("--max-steps", type=int, default=15)
    parser.add_argument("--max-validation-retries", type=int, default=3)
    parser.add_argument("--state-wait", type=float, default=20.0)
    parser.add_argument("--observation-wait", type=float, default=10.0)
    parser.add_argument("--eval-windows", default="off")
    parser.add_argument("--model", default="")
    parser.add_argument("--base-url", default="")
    parser.add_argument("--stream-timeout", type=float, default=300.0)
    parser.add_argument("--max-tool-rounds", type=int, default=None)
    parser.add_argument("--improve-threshold", type=float, default=None)
    parser.add_argument("--degrade-threshold", type=float, default=None)
    parser.add_argument("--qos-sample-count", type=int, default=None)
    parser.add_argument("--qos-sample-interval", type=float, default=None)
    parser.add_argument("--timeout", type=float, default=720.0)
    parser.add_argument("--attempts-per-trial", type=int, default=2)
    parser.add_argument("--retry-delay", type=float, default=5.0)
    parser.add_argument("--max-infra-failures", type=int, default=5)
    parser.add_argument(
        "--broadcast-workers",
        type=int,
        default=1,
        help="批量实验默认串行广播，降低外部 LLM API 并发连接错误；可设为 3 恢复并发。",
    )
    parser.add_argument(
        "--no-reset-baseline",
        action="store_true",
        help="默认每次 attempt 前恢复 ns-3 基线；该选项用于显式关闭。",
    )
    parser.add_argument("--reset-timeout", type=float, default=10.0)
    parser.add_argument("--reset-settle", type=float, default=2.0)
    parser.add_argument("--reset-attempts", type=int, default=3)
    parser.add_argument("--reset-retry-delay", type=float, default=1.0)
    parser.add_argument("--reset-stable-timeout", type=float, default=30.0)
    parser.add_argument("--reset-poll-interval", type=float, default=1.0)
    parser.add_argument("--reset-state-timeout", type=float, default=3.0)
    parser.add_argument("--reset-state-max-age", type=float, default=5.0)
    parser.add_argument("--no-wait-stable-baseline", action="store_true")
    parser.add_argument("--out-dir", default="")
    parser.add_argument("--plot-only", default="")
    parser.add_argument("--no-plots", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--stop-on-failure", action="store_true")
    parser.add_argument(
        "--seed-memory-db",
        default="",
        help="只用于 memory=on 条件的历史记忆 SQLite DB；目标 DB 不存在时复制作为初始记忆。",
    )
    parser.add_argument(
        "--reseed-memory",
        action="store_true",
        help="与 --seed-memory-db 配合使用，强制覆盖 memory=on 条件的现有 DB；不要和 --resume 混用。",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="若 results.csv 已存在，则按每个 profile/memory 已完成的有效行数继续补足 repetitions。",
    )
    args = parser.parse_args()

    if args.plot_only:
        csv_path = Path(args.plot_only)
        out_dir = csv_path.parent
        summarize(csv_path, out_dir)
        plot(csv_path, out_dir)
        print(f"[experiment] plot-only done: {out_dir}", flush=True)
        return

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = Path(args.out_dir) if args.out_dir else REPO_ROOT / "logs" / "experiments" / f"tool_memory_{stamp}"
    csv_path = out_dir / "results.csv"
    profiles = [item.strip() for item in args.profiles.split(",") if item.strip()]
    memories = [item.strip() for item in args.memories.split(",") if item.strip()]
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "config.json").write_text(
        json.dumps(vars(args), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    completed_by_condition: dict[tuple[str, str], int] = defaultdict(int)
    last_trial_by_condition: dict[tuple[str, str], int] = defaultdict(int)
    if args.resume and csv_path.exists():
        for row in read_rows(csv_path):
            key = (row.get("profile", ""), row.get("memory", ""))
            if key[0] not in profiles or key[1] not in memories:
                continue
            try:
                trial_num = int(row.get("trial") or 0)
            except ValueError:
                trial_num = 0
            last_trial_by_condition[key] = max(last_trial_by_condition[key], trial_num)
            if row.get("run_status") == "completed":
                completed_by_condition[key] += 1
        summarize(csv_path, out_dir)

    print(f"[experiment] output={out_dir}", flush=True)
    if args.resume:
        print(
            "[experiment] resume counts="
            + json.dumps(
                {f"{p}/{m}": completed_by_condition[(p, m)] for p in profiles for m in memories},
                ensure_ascii=False,
            ),
            flush=True,
        )
    total = len(profiles) * len(memories) * args.repetitions
    index = 0
    for profile in profiles:
        for memory in memories:
            key = (profile, memory)
            if args.reseed_memory and args.resume:
                raise SystemExit("--reseed-memory cannot be combined with --resume")
            seed_memory_db(args, out_dir, profile, memory)
            completed = completed_by_condition[key] if args.resume else 0
            infra_failures = 0
            trial = last_trial_by_condition[key] if args.resume else 0
            while completed < args.repetitions:
                trial += 1
                index += 1
                print(
                    f"[experiment] target={completed + 1}/{args.repetitions} "
                    f"overall~{min(index, total)}/{total} profile={profile} memory={memory} "
                    f"trial={trial}",
                    flush=True,
                )
                row: dict[str, object] | None = None
                for attempt in range(1, max(1, args.attempts_per_trial) + 1):
                    try:
                        reset_ns3_baseline(
                            args,
                            session_id=(
                                f"experiment-reset-{profile}-{memory}-"
                                f"t{trial:02d}-a{attempt:02d}"
                            ),
                        )
                    except RuntimeError as exc:
                        row = reset_failure_row(
                            args, profile, memory, trial, out_dir,
                            attempt=attempt, error=exc,
                        )
                    else:
                        row = run_one(args, profile, memory, trial, out_dir, attempt=attempt)
                    row["attempts"] = attempt
                    row["infra_retries"] = attempt - 1
                    if row.get("run_status") not in {"infra_error", "infra_timeout"}:
                        break
                    if attempt < max(1, args.attempts_per_trial):
                        print(
                            f"[experiment] infra retry profile={profile} memory={memory} "
                            f"trial={trial} attempt={attempt} status={row.get('run_status')}",
                            flush=True,
                        )
                        time.sleep(max(0.0, args.retry_delay))
                assert row is not None
                append_csv(csv_path, row)
                print(
                    f"[experiment] status={row.get('run_status')} outcome={row.get('outcome')} "
                    f"turns={row.get('turns')} score={row.get('effect_score')} "
                    f"rc={row.get('returncode')} attempts={row.get('attempts')}",
                    flush=True,
                )
                if row.get("run_status") == "completed":
                    completed += 1
                elif row.get("run_status") in {"infra_error", "infra_timeout"}:
                    infra_failures += 1
                    if infra_failures > args.max_infra_failures:
                        raise SystemExit(
                            f"too many infrastructure failures for {profile}/{memory}: {infra_failures}"
                        )
                else:
                    raise SystemExit(f"non-infrastructure run failed: {row}")
                if args.stop_on_failure and row.get("run_status") == "run_error":
                    raise SystemExit(f"run failed: {row}")
                summary = summarize(csv_path, out_dir)
                (out_dir / "latest_summary.json").write_text(
                    json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )

    summary = summarize(csv_path, out_dir)
    if not args.no_plots:
        plot(csv_path, out_dir)
    print(f"[experiment] complete rows={summary['rows']} csv={csv_path}", flush=True)
    print(f"[experiment] summary={out_dir / 'summary.json'}", flush=True)
    if args.no_plots:
        print("[experiment] plots=skipped (--no-plots)", flush=True)
    else:
        print(f"[experiment] plots={out_dir / 'summary_bars.png'}, {out_dir / 'memory_advantage.png'}", flush=True)


if __name__ == "__main__":
    main()
