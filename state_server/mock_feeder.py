"""
Mock 遥测喂数器 —— 仅用于 --mock 演示。

mock 模式下没有真实香蕉派持续上报，state server 的 /history 为空，
导致 academic_plot 窗口与 dashboard 曲线无数据。本模块在后台守护线程里
周期性地把（带随机扰动的）mock 状态 POST 到 state server /state，使曲线
"活起来"；协商完成后调用 apply_decision() 注入最终参数并把性能指标朝
改善方向移动，让曲线体现协商前后的变化。

上报时 source 固定为 "mock"，因此 state server 需以 --allow-mock 启动。
"""
from __future__ import annotations

import copy
import random
import threading
from datetime import datetime, timezone

import requests

from src.tools.edca import cw_to_ecw

AP_IDS = ("ap1", "ap2", "ap3")
_PERF_FIELDS = ("throughput_mbps_iperf", "throughput_mbps_user", "latency_ms", "packet_loss_pct")


class MockTelemetryFeeder:
    """周期性向 state server 喂送带扰动的 mock 遥测。线程安全。"""

    def __init__(self, server: str, ap_state: dict, interval: float = 1.0):
        self.server = server.rstrip("/")
        self.interval = max(0.2, interval)
        # 深拷贝：喂数器在自己的副本上演化，不影响协商输入
        self._state = copy.deepcopy(ap_state)
        # 性能目标基线（协商后 apply_decision 会朝改善方向调整）
        self._perf_target = {
            ap: {f: float(self._state[ap].get(f, 0.0)) for f in _PERF_FIELDS}
            for ap in AP_IDS
        }
        self._http = requests.Session()
        self._http.trust_env = False
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ── 生命周期 ────────────────────────────────────────────────────────
    def start(self) -> None:
        self._post_once()  # 先推一帧，曲线立即有起点
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name="mock-feeder"
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)

    # ── 协商后注入决策 ──────────────────────────────────────────────────
    def apply_decision(self, decision: dict) -> None:
        """注入最终 MAC 参数，并把性能目标朝改善方向移动。"""
        if not decision:
            return
        with self._lock:
            for ap in AP_IDS:
                params = decision.get(ap) or decision.get(ap.upper()) or {}
                for key, cast in (
                    ("tx_power_dbm", float), ("cwmin", int),
                    ("cwmax", int), ("aifsn", int),
                ):
                    if params.get(key) is not None:
                        try:
                            value = cast(params[key])
                            # 决策为实际 CW 值；喂回的“上报数据”与真实上报一致用指数 n。
                            if key in ("cwmin", "cwmax"):
                                value = cw_to_ecw(value)
                            self._state[ap][key] = value
                        except (TypeError, ValueError):
                            pass
                # 协商后预期：吞吐 +12%、延迟 -25%、丢包 -40%
                t = self._perf_target[ap]
                t["throughput_mbps_iperf"] *= 1.12
                t["throughput_mbps_user"] *= 1.12
                t["latency_ms"] *= 0.75
                t["packet_loss_pct"] *= 0.60

    # ── 内部 ───────────────────────────────────────────────────────────
    def _loop(self) -> None:
        while not self._stop.wait(self.interval):
            self._post_once()

    def _post_once(self) -> None:
        ts = datetime.now(timezone.utc).isoformat()
        with self._lock:
            payloads = [self._sample(ap) for ap in AP_IDS]
        for payload in payloads:
            payload["timestamp"] = ts
            try:
                self._http.post(f"{self.server}/state", json=payload, timeout=2)
            except Exception:
                pass  # 演示用途，喂数失败静默忽略

    def _sample(self, ap: str) -> dict:
        """演化一帧：性能指标向 target 缓慢逼近并加随机抖动。需持锁调用。"""
        base = self._state[ap]
        target = self._perf_target[ap]
        for f in _PERF_FIELDS:
            cur = float(base.get(f, target[f]))
            nxt = cur + (target[f] - cur) * 0.3
            nxt += nxt * random.uniform(-0.04, 0.04)
            base[f] = max(0.0, nxt)  # 演化结果写回，下一帧从此继续
        payload = copy.deepcopy(base)  # 含全部 REQUIRED_FIELDS
        for f in _PERF_FIELDS:
            payload[f] = round(payload[f], 2)
        payload["ap_id"] = ap
        payload["source"] = "mock"
        return payload
