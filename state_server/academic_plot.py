"""
Matplotlib academic-style live plot window for AP state history.

Usage:
  python state_server/academic_plot.py
  python state_server/academic_plot.py --server http://localhost:5001 --window 25

The x-axis is a fixed sliding window. During the first 25 seconds, curves fill
from left to right. After that, only the latest 25 seconds are shown and older
samples are discarded from the visible plot.
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime
from typing import Any

import requests

AP_IDS = ("ap1", "ap2", "ap3")
AP_COLORS = {
    "ap1": "#1f77b4",
    "ap2": "#d62728",
    "ap3": "#2ca02c",
}
AP_MARKERS = {
    "ap1": "o",
    "ap2": "s",
    "ap3": "^",
}
AP_LINESTYLES = {
    "ap1": "-",
    "ap2": "--",
    "ap3": "-.",
}

VIEW_FIELDS = {
    "Throughput iperf": ("throughput_mbps_iperf", "Throughput iperf", "Mbps"),
    "Throughput user": ("throughput_mbps_user", "Throughput user", "Mbps"),
    "Throughput total": ("throughput_mbps_total", "Throughput total", "Mbps"),
    "Latency": ("latency_ms", "Latency", "ms"),
    "Packet loss": ("packet_loss_pct", "Packet loss", "%"),
    "Txpower": ("tx_power_dbm", "Tx power", "dBm"),
    "CWmin": ("cwmin", "CWmin", "value"),
    "CWmax": ("cwmax", "CWmax", "value"),
    "AIFSN": ("aifsn", "AIFSN", "value"),
}
VIEW_LABELS = tuple(VIEW_FIELDS)


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def history_origin(history: dict[str, list[dict[str, Any]]]) -> datetime | None:
    timestamps: list[datetime] = []
    for ap in AP_IDS:
        for row in history.get(ap, []):
            ts = _parse_ts(row.get("t"))
            if ts is not None:
                timestamps.append(ts)
    return min(timestamps) if timestamps else None


def latest_elapsed_seconds(history: dict[str, list[dict[str, Any]]], origin: datetime) -> float:
    latest = 0.0
    for ap in AP_IDS:
        for row in history.get(ap, []):
            ts = _parse_ts(row.get("t"))
            if ts is None:
                continue
            latest = max(latest, (ts - origin).total_seconds())
    return max(0.0, latest)


def sliding_series(
    rows: list[dict[str, Any]],
    field: str,
    origin: datetime,
    latest_elapsed: float,
    window_seconds: float,
) -> tuple[list[float], list[float]]:
    window_start = max(0.0, latest_elapsed - window_seconds)
    xs: list[float] = []
    ys: list[float] = []
    for row in rows:
        ts = _parse_ts(row.get("t"))
        y = row.get(field)
        if ts is None or y is None:
            continue
        elapsed = (ts - origin).total_seconds()
        x = elapsed - window_start
        if 0.0 <= x <= window_seconds:
            xs.append(x)
            ys.append(float(y))
    return xs, ys


def fetch_history(server: str, timeout: float = 2.0) -> dict[str, list[dict[str, Any]]]:
    resp = requests.get(f"{server.rstrip('/')}/history", timeout=timeout)
    resp.raise_for_status()
    data = resp.json()
    return {ap: list(data.get(ap, [])) for ap in AP_IDS}


def _require_matplotlib():
    # 仅 Linux 依赖 X11 的 DISPLAY；macOS 用原生 macosx 后端，Windows 用 win32。
    if sys.platform.startswith("linux") and not os.environ.get("DISPLAY"):
        raise SystemExit(
            "No desktop display is available: DISPLAY is not set. "
            "A MATLAB-style Matplotlib figure requires a GUI session. "
            "Run from a desktop terminal, VNC session, or SSH with X forwarding."
        )

    try:
        import matplotlib
        if not os.environ.get("MPLBACKEND") and sys.platform.startswith("linux"):
            matplotlib.use("TkAgg")
        import matplotlib.pyplot as plt
        from matplotlib.widgets import RadioButtons
    except ModuleNotFoundError as exc:
        raise SystemExit(
            "matplotlib is not installed. Install it with: python -m pip install matplotlib"
        ) from exc
    except Exception as exc:
        raise SystemExit(
            f"Failed to initialize a desktop Matplotlib backend: {exc}. "
            "Install tkinter/Qt support or set MPLBACKEND to an installed GUI backend such as TkAgg or QtAgg."
        ) from exc

    backend = matplotlib.get_backend().lower()
    if backend in {"agg", "webagg"}:
        raise SystemExit(
            f"Matplotlib selected non-desktop backend {matplotlib.get_backend()!r}. "
            "Use a GUI backend such as TkAgg or QtAgg for a MATLAB-style figure window."
        )
    return plt, RadioButtons


def run_plot(server: str, window_seconds: float, interval_seconds: float) -> None:
    plt, RadioButtons = _require_matplotlib()

    plt.rcParams.update({
        "figure.facecolor": "white",
        "axes.facecolor": "white",
        "axes.edgecolor": "#333333",
        "axes.grid": True,
        "grid.color": "#d9d9d9",
        "grid.linestyle": "--",
        "grid.linewidth": 0.7,
        "font.size": 10,
        "axes.titlesize": 12,
        "axes.labelsize": 10,
        "legend.fontsize": 9,
        "lines.linewidth": 2.0,
        "lines.markersize": 4,
    })

    fig = plt.figure(figsize=(10.8, 6.2))
    fig.canvas.manager.set_window_title("Multi-AP Academic Curves")
    ax = fig.add_axes((0.22, 0.13, 0.74, 0.77))
    radio_ax = fig.add_axes((0.03, 0.25, 0.15, 0.50), facecolor="#f7f7f7")
    radio = RadioButtons(radio_ax, VIEW_LABELS, active=0)
    status = fig.text(0.22, 0.965, "Waiting for data...", color="#555555")
    state = {"view": VIEW_LABELS[0], "origin": None, "last_error": ""}

    def style_axis(ax, title: str, ylabel: str) -> None:
        ax.set_title(title)
        ax.set_xlim(0, window_seconds)
        ax.set_xlabel("Time in sliding window (s)")
        ax.set_ylabel(ylabel)
        ax.spines["top"].set_visible(False)
        ax.spines["right"].set_visible(False)

    def plot_field(history, field: str, title: str, ylabel: str, origin, latest_elapsed) -> None:
        ax.clear()
        for ap in AP_IDS:
            xs, ys = sliding_series(history.get(ap, []), field, origin, latest_elapsed, window_seconds)
            ax.plot(
                xs,
                ys,
                marker=AP_MARKERS[ap],
                linestyle=AP_LINESTYLES[ap],
                color=AP_COLORS[ap],
                label=ap.upper(),
            )
        style_axis(ax, title, ylabel)
        ax.legend(loc="best", frameon=False, ncol=3)

    def redraw() -> bool:
        try:
            history = fetch_history(server)
            if state["origin"] is None:
                state["origin"] = history_origin(history)
            origin = state["origin"]
            if origin is None:
                ax.clear()
                ax.text(0.5, 0.5, "No samples yet", ha="center", va="center", transform=ax.transAxes)
                ax.set_axis_off()
                status.set_text(f"Server: {server} | window: {window_seconds:g}s | waiting for samples")
                fig.canvas.draw_idle()
                return True

            latest = latest_elapsed_seconds(history, origin)
            window_start = max(0.0, latest - window_seconds)
            view = state["view"]
            field, title, ylabel = VIEW_FIELDS[view]
            plot_field(history, field, title, ylabel, origin, latest)

            status.set_text(
                f"Server: {server} | visible window: {window_start:.1f}-{window_start + window_seconds:.1f}s "
                f"mapped to x=0-{window_seconds:g}s"
            )
            state["last_error"] = ""
        except Exception as exc:  # keep the window alive if one fetch fails
            state["last_error"] = str(exc)
            status.set_text(f"Fetch error: {exc}")
        fig.canvas.draw_idle()
        return True

    def on_view(label: str) -> None:
        state["view"] = label
        redraw()

    radio.on_clicked(on_view)
    redraw()
    timer = fig.canvas.new_timer(interval=int(interval_seconds * 1000))
    timer.add_callback(redraw)
    timer.start()
    plt.show()


def main() -> None:
    parser = argparse.ArgumentParser(description="弹出 Matplotlib 学术风格 AP 曲线窗口")
    parser.add_argument("--server", default="http://localhost:5001")
    parser.add_argument("--window", type=float, default=25.0, help="固定滑动窗口长度，单位秒")
    parser.add_argument("--interval", type=float, default=1.0, help="刷新间隔，单位秒")
    args = parser.parse_args()
    run_plot(args.server, args.window, args.interval)


if __name__ == "__main__":
    main()
