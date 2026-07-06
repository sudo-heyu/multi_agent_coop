"""
ns-3 桥 —— 把真实 ns-3 仿真接到既有框架的两个 HTTP 边界上。

它做两件事，协商内核/agent/dashboard 全部不用改：

  1) 数据来源：用 subprocess 启动 ns-3 场景（scratch/multiap_coop，--live 模式），
     读它 stdout 的 `TELEMETRY {json}` 行 → 换算 → 定时 POST 到 state server /state，
     source="ns3"（不在 server 的生成数据黑名单里，直接按真实来源放行）。

  2) 决策下发：本进程用 Flask 暴露 /apply（替代香蕉派 executor）。协商结束后
     orchestration 会 POST 决策到这里；桥把参数写成一行 `APPLY ...` 命令喂进
     ns-3 的 stdin，实时改运行中仿真的 EDCA / 发射功率。

单位约定（与 mock_feeder / executor 一致）：
  - 系统内 cwmin/cwmax 上报与下发都用【指数 n】（CW = 2^n - 1）；
  - ns-3 侧用【实际 CW 值】。
  故：上报时 实际CW → cw_to_ecw → 指数；下发时 指数 → ecw_to_cw → 实际CW。

用法：
  # 先确保 state server 在跑（真实模式即可，无需 --allow-mock）：
  #   python state_server/server.py
  # 再启动桥（会自己拉起 ns-3）：
  #   python state_server/ns3_bridge.py
  # 然后把 ap_endpoints.json 里三个 AP 都指向 http://localhost:5003
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path

import requests
from flask import Flask, request, jsonify

# 允许以 `python state_server/ns3_bridge.py` 从项目根运行时导入 src.*
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src.tools.edca import cw_to_ecw, ecw_to_cw  # noqa: E402

VALID_AP_IDS = {"ap1", "ap2", "ap3"}
# state server REQUIRED_FIELDS 里的指标键（ap_id/timestamp 之外，ns3 遥测都带）
TELEMETRY_KEYS = {
    "tx_power_dbm", "cwmin", "cwmax", "aifsn",
    "Data_rate_to_bandwidth_ratio", "tx_retries_ratio",
    "neighbor_rssi_dbm", "sta_rssi_dbm", "noise_floor_dbm",
    "throughput_mbps_iperf", "throughput_mbps_user", "ac_iperf", "ac_user",
    "latency_ms", "packet_loss_pct",
    # 协议级 Co-SR 观测字段（各启用 MAPC 方案追加，缺省来源可能没有）
    "bss_color", "obss_pd_dbm", "sr_reset_count",
}


def _passthru(v):
    return float(v)


# 策略 → [(提案字段, APPLY 键, 值转换器)] 映射。新增一种 MAPC = 加一行表项，
# apply() 据此把决策 params 拼成 `APPLY <ap> k=v ...` 命令。cwmin/cwmax 的
# 转换器把系统内的【指数 n】还原成 ns-3 侧的【实际 CW 值】。
STRATEGY_KEYS: dict[str, list[tuple[str, str, object]]] = {
    "co_sr": [
        ("tx_power_dbm", "tx", _passthru),
        ("obss_pd_dbm", "obss_pd", _passthru),
    ],
    "co_edca": [
        ("CWmin", "cwmin", lambda v: ecw_to_cw(int(v))),
        ("CWmax", "cwmax", lambda v: ecw_to_cw(int(v))),
        ("AIFSN", "aifsn", lambda v: int(v)),
    ],
}
# joint = co_sr ∪ co_edca
STRATEGY_KEYS["joint"] = STRATEGY_KEYS["co_sr"] + STRATEGY_KEYS["co_edca"]


class Ns3Bridge:
    """管理 ns-3 子进程：读遥测转发到 state server，写 APPLY 命令下发决策。"""

    def __init__(self, ns3_dir: str, state_server: str, sim_time: float,
                 report_interval: float, scenario: str = "line", extra_args: str = ""):
        self.state_server = state_server.rstrip("/")
        self._http = requests.Session()
        self._http.trust_env = False
        self._stdin_lock = threading.Lock()
        self._seen: set[str] = set()   # 已上报过至少一帧的 ap；首帧丢弃（含暖机增量）

        cmdline = (
            f"scratch/multiap_coop/multiap_coop --live --scenario={scenario} "
            f"--simTime={sim_time} --reportInterval={report_interval} {extra_args}"
        ).strip()
        print(f"[bridge] 启动 ns-3: {cmdline}")
        self.proc = subprocess.Popen(
            ["./ns3", "run", cmdline, "--no-build"],
            cwd=ns3_dir,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=None,               # ns-3 的 [live]/[apply] 日志直接透传到终端
            text=True,
            bufsize=1,                 # 行缓冲
        )

    # ── 遥测读取（后台线程）────────────────────────────────────────────
    def start_reader(self) -> None:
        threading.Thread(target=self._read_loop, daemon=True, name="ns3-reader").start()

    def _read_loop(self) -> None:
        assert self.proc.stdout is not None
        for line in self.proc.stdout:
            line = line.strip()
            if not line.startswith("TELEMETRY "):
                continue               # 忽略构建/仿真的其它输出
            try:
                obj = json.loads(line[len("TELEMETRY "):])
            except json.JSONDecodeError:
                continue
            self._forward(obj)
        print("[bridge] ns-3 stdout 结束，读取线程退出")

    def _forward(self, obj: dict) -> None:
        ap_id = obj.get("ap_id")
        if ap_id not in VALID_AP_IDS:
            return
        # 丢弃每个 AP 的首帧：其增量含 ~2s 暖机字节，吞吐会偏高失真
        if ap_id not in self._seen:
            self._seen.add(ap_id)
            return
        # 实际 CW → 指数 n（与真实 AP 上报口径一致）
        try:
            obj["cwmin"] = cw_to_ecw(int(obj["cwmin"]))
            obj["cwmax"] = cw_to_ecw(int(obj["cwmax"]))
        except (KeyError, TypeError, ValueError):
            pass
        payload = {k: obj.get(k) for k in TELEMETRY_KEYS}
        payload["ap_id"] = ap_id
        payload["timestamp"] = datetime.now(timezone.utc).isoformat()
        payload["source"] = "ns3"
        try:
            self._http.post(f"{self.state_server}/state", json=payload, timeout=2)
        except requests.RequestException as exc:
            print(f"[bridge] 上报 {ap_id} 失败: {exc}")

    # ── 决策下发 ────────────────────────────────────────────────────────
    def apply(self, ap_id: str, strategy: str, params: dict) -> dict:
        """把一条决策转成 `APPLY ...` 命令写进 ns-3 stdin。返回执行摘要。

        由 STRATEGY_KEYS 表驱动：新增一种 MAPC 只需在表里加一行，此处零改动。
        """
        if strategy not in STRATEGY_KEYS:
            return {
                "ok": False,
                "error": f"unsupported strategy: {strategy!r}",
                "command": "",
                "details": {},
                "http_status": 400,
            }

        parts = [f"APPLY {ap_id}"]
        details: dict[str, object] = {}

        for field, ns3_key, conv in STRATEGY_KEYS.get(strategy, []):
            # 提案字段名优先，回退其小写别名（如 CWmin / cwmin 都接受）
            raw = params.get(field, params.get(field.lower()))
            if raw is None:
                continue
            try:
                value = conv(raw)
            except (TypeError, ValueError):
                continue
            parts.append(f"{ns3_key}={value}")
            details[ns3_key] = value

        if not details:
            return {
                "ok": False,
                "error": f"no recognized params for strategy {strategy!r}",
                "command": "",
                "details": {},
                "http_status": 400,
            }

        cmd = " ".join(parts) + "\n"
        poll = getattr(self.proc, "poll", None)
        if callable(poll) and poll() is not None:
            return {
                "ok": False,
                "error": "ns-3 process is not running",
                "command": cmd.strip(),
                "details": details,
                "http_status": 503,
            }

        with self._stdin_lock:
            if self.proc.stdin is None:
                return {
                    "ok": False,
                    "error": "ns-3 stdin is not available",
                    "command": cmd.strip(),
                    "details": details,
                    "http_status": 503,
                }
            try:
                self.proc.stdin.write(cmd)
                self.proc.stdin.flush()
            except OSError as exc:
                return {
                    "ok": False,
                    "error": f"failed to write to ns-3 stdin: {exc}",
                    "command": cmd.strip(),
                    "details": details,
                    "http_status": 503,
                }
        print(f"[bridge] 下发 → ns-3: {cmd.strip()}")
        return {"ok": True, "command": cmd.strip(), "details": details}


# ── Flask：暴露 /apply，替代香蕉派 executor ─────────────────────────────
app = Flask(__name__)
_bridge: Ns3Bridge | None = None
_last_result: dict = {}


@app.route("/apply", methods=["POST"])
def apply_endpoint():
    body = request.get_json(silent=True)
    if not body:
        return jsonify({"ok": False, "error": "body must be JSON"}), 400
    ap_id = body.get("ap_id", "")
    strategy = body.get("strategy", "")
    params = body.get("params", {})
    session_id = body.get("session_id", "")
    if ap_id not in VALID_AP_IDS:
        return jsonify({"ok": False, "error": f"unknown ap_id: {ap_id!r}"}), 400
    if not strategy:
        return jsonify({"ok": False, "error": "missing strategy"}), 400
    if not params:
        return jsonify({"ok": False, "error": "missing params"}), 400

    if _bridge is None:
        return jsonify({"ok": False, "error": "bridge is not initialized"}), 503
    result = _bridge.apply(ap_id, strategy, params)
    result.update({"ap_id": ap_id, "session_id": session_id,
                   "applied_at": datetime.now(timezone.utc).isoformat()})
    global _last_result
    _last_result = result
    http_status = result.pop("http_status", 200 if result["ok"] else 500)
    return jsonify(result), http_status


@app.route("/status", methods=["GET"])
def status():
    return jsonify(_last_result or {"status": "idle"}), 200


@app.route("/health", methods=["GET"])
def health():
    alive = _bridge is not None and _bridge.proc.poll() is None
    return jsonify({"ok": True, "ns3_alive": alive}), 200


def main():
    parser = argparse.ArgumentParser(description="ns-3 ↔ 框架 桥")
    parser.add_argument("--ns3-dir", default="/Users/heyu/Developer/ns-3.47",
                        help="ns-3 根目录（含 ./ns3 与已编译的 multiap_coop）")
    parser.add_argument("--state-server", default="http://localhost:5001",
                        help="state server 地址")
    parser.add_argument("--sim-time", type=float, default=3600.0,
                        help="ns-3 场景运行时长(s)，默认 1 小时")
    parser.add_argument("--report-interval", type=float, default=1.0,
                        help="遥测采样间隔(s)")
    parser.add_argument("--scenario", default="line",
                        help="拓扑场景: line | triangle（见 multiap_coop.cc BuildScenario）")
    parser.add_argument("--port", type=int, default=5003,
                        help="桥的 /apply 监听端口")
    parser.add_argument("--ns3-args", default="",
                        help="透传给 ns-3 场景的额外参数，如 '--txPowerDbm=20 --userRateMbps=3'")
    args = parser.parse_args()

    global _bridge
    _bridge = Ns3Bridge(args.ns3_dir, args.state_server, args.sim_time,
                        args.report_interval, args.scenario, args.ns3_args)
    _bridge.start_reader()

    print(f"[bridge] /apply 监听 http://0.0.0.0:{args.port} —— 请把 ap_endpoints.json "
          f"三个 AP 都指向 http://localhost:{args.port}")
    app.run(host="0.0.0.0", port=args.port, debug=False)


if __name__ == "__main__":
    main()
