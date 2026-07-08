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
  # 然后把 config/ap_endpoints.json 里三个 AP 都指向 http://localhost:5003
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from flask import Flask, request, jsonify

# 允许以 `python state_server/ns3_bridge.py` 从项目根运行时导入 src.*
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src.tools.edca import cw_to_ecw, ecw_to_cw  # noqa: E402
from src.sta_feedback import summarize_ap_feedback  # noqa: E402

VALID_AP_IDS = {"ap1", "ap2", "ap3"}
DEFAULT_PRIORITIES = {"ap1": "low", "ap2": "high", "ap3": "low"}
DEFAULT_SERVICE_NAMES = {
    "ap1": "background_download",
    "ap2": "live_streaming",
    "ap3": "background_download",
}
DEFAULT_BUSINESS_TYPES = {"ap1": "后台下载", "ap2": "直播", "ap3": "后台下载"}
# state server REQUIRED_FIELDS 里的指标键（ap_id/timestamp 之外，ns3 遥测都带）
TELEMETRY_KEYS = {
    "service_name", "business_type", "traffic_priority",
    "tx_power_dbm", "cwmin", "cwmax", "aifsn",
    "be_cwmin", "be_cwmax", "be_aifsn",
    "vi_cwmin", "vi_cwmax", "vi_aifsn",
    "Data_rate_to_bandwidth_ratio", "tx_retries_ratio",
    "neighbor_rssi_dbm", "sta_rssi_dbm", "noise_floor_dbm",
    "throughput_mbps_iperf", "throughput_mbps_user", "ac_iperf", "ac_user",
    "latency_ms", "jitter_ms", "packet_loss_pct",
    "stas", "sta_feedback_summary", "sla_violations",
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
        ("BE_CWmin", "be_cwmin", lambda v: ecw_to_cw(int(v))),
        ("BE_CWmax", "be_cwmax", lambda v: ecw_to_cw(int(v))),
        ("BE_AIFSN", "be_aifsn", lambda v: int(v)),
        ("VI_CWmin", "vi_cwmin", lambda v: ecw_to_cw(int(v))),
        ("VI_CWmax", "vi_cwmax", lambda v: ecw_to_cw(int(v))),
        ("VI_AIFSN", "vi_aifsn", lambda v: int(v)),
    ],
}
# joint = co_sr ∪ co_edca
STRATEGY_KEYS["joint"] = STRATEGY_KEYS["co_sr"] + STRATEGY_KEYS["co_edca"]

APPLY_RAW_KEYS = {
    "tx": "tx_power_dbm",
    "obss_pd": "obss_pd_dbm",
}


def _parse_ap_value_map(
    spec: str,
    *,
    defaults: dict[str, str],
    allowed_values: set[str] | None = None,
) -> dict[str, str]:
    values = dict(defaults)
    if not (spec or "").strip():
        return values
    for part in spec.split(","):
        if not part.strip():
            continue
        if "=" not in part:
            raise argparse.ArgumentTypeError(f"缺少 '=': {part!r}")
        raw_ap, raw_value = part.split("=", 1)
        ap_id = raw_ap.strip().lower()
        value = raw_value.strip()
        if ap_id not in VALID_AP_IDS:
            raise argparse.ArgumentTypeError(f"未知 AP: {ap_id!r}")
        if not value:
            raise argparse.ArgumentTypeError(f"{ap_id} 的值不能为空")
        if allowed_values is not None and value.lower() not in allowed_values:
            allowed = ",".join(sorted(allowed_values))
            raise argparse.ArgumentTypeError(f"{ap_id}={value!r} 非法，允许值: {allowed}")
        values[ap_id] = value.lower() if allowed_values is not None else value
    return values


def _value_matches(actual: object, expected: object, *, tol: float = 1e-6) -> bool:
    try:
        return abs(float(actual) - float(expected)) <= tol
    except (TypeError, ValueError):
        return actual == expected


def _matches_apply_details(raw: dict, details: dict) -> tuple[bool, list[dict]]:
    mismatches: list[dict] = []
    for apply_key, expected in details.items():
        raw_key = APPLY_RAW_KEYS.get(apply_key, apply_key)
        if raw_key not in raw:
            mismatches.append({"key": raw_key, "expected": expected, "actual": None})
            continue
        actual = raw.get(raw_key)
        if not _value_matches(actual, expected):
            mismatches.append({"key": raw_key, "expected": expected, "actual": actual})
    return not mismatches, mismatches


class Ns3Bridge:
    """管理 ns-3 子进程：读遥测转发到 state server，写 APPLY 命令下发决策。"""

    def __init__(self, ns3_dir: str, state_server: str, sim_time: float,
                 report_interval: float, scenario: str = "line", extra_args: str = "",
                 priorities: dict[str, str] | None = None,
                 service_names: dict[str, str] | None = None,
                 business_types: dict[str, str] | None = None,
                 apply_ack_timeout: float = 3.0):
        self.state_server = state_server.rstrip("/")
        self._http = requests.Session()
        self._http.trust_env = False
        self._stdin_lock = threading.Lock()
        self._seen: set[str] = set()   # 已上报过至少一帧的 ap；首帧丢弃（含暖机增量）
        self._priorities = priorities or DEFAULT_PRIORITIES
        self._service_names = service_names or DEFAULT_SERVICE_NAMES
        self._business_types = business_types or DEFAULT_BUSINESS_TYPES
        self._apply_ack_timeout = max(0.0, float(apply_ack_timeout))
        self._telemetry_cond = threading.Condition()
        self._telemetry_seq: dict[str, int] = {ap: 0 for ap in VALID_AP_IDS}
        self._last_raw_by_ap: dict[str, dict] = {}
        self._last_payload_by_ap: dict[str, dict] = {}
        self._sta_feedback_by_ap: dict[str, list[dict]] = {ap: [] for ap in VALID_AP_IDS}
        self._last_telemetry_at: str | None = None
        self._last_post_error: dict | None = None
        self._last_apply: dict | None = None

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
            if line.startswith("STA_TELEMETRY "):
                try:
                    obj = json.loads(line[len("STA_TELEMETRY "):])
                except json.JSONDecodeError:
                    continue
                self._forward_sta(obj)
            elif line.startswith("TELEMETRY "):
                try:
                    obj = json.loads(line[len("TELEMETRY "):])
                except json.JSONDecodeError:
                    continue
                self._forward(obj)
        print("[bridge] ns-3 stdout 结束，读取线程退出")

    def _forward_sta(self, obj: dict) -> None:
        ap_id = str(obj.get("ap_id") or obj.get("associated_ap") or "").lower()
        if ap_id not in VALID_AP_IDS:
            return
        sta_id = str(obj.get("sta_id") or f"{ap_id}_sta")
        entry = dict(obj)
        entry["sta_id"] = sta_id
        entry["associated_ap"] = ap_id
        current = list(self._sta_feedback_by_ap.get(ap_id) or [])
        current = [item for item in current if item.get("sta_id") != sta_id]
        current.append(entry)
        self._sta_feedback_by_ap[ap_id] = current

    def _forward(self, obj: dict) -> None:
        ap_id = obj.get("ap_id")
        if ap_id not in VALID_AP_IDS:
            return
        # 丢弃每个 AP 的首帧：其增量含 ~2s 暖机字节，吞吐会偏高失真
        if ap_id not in self._seen:
            self._seen.add(ap_id)
            return
        raw_obj = dict(obj)
        # 实际 CW → 指数 n（与真实 AP 上报口径一致）
        for key in ("cwmin", "cwmax", "be_cwmin", "be_cwmax", "vi_cwmin", "vi_cwmax"):
            try:
                if obj.get(key) is not None:
                    obj[key] = cw_to_ecw(int(obj[key]))
            except (TypeError, ValueError):
                pass
        service_names = getattr(self, "_service_names", DEFAULT_SERVICE_NAMES)
        business_types = getattr(self, "_business_types", DEFAULT_BUSINESS_TYPES)
        priorities = getattr(self, "_priorities", DEFAULT_PRIORITIES)
        obj.setdefault("service_name", service_names.get(ap_id, "ns3_service"))
        obj.setdefault("business_type", business_types.get(ap_id, "ns-3业务"))
        obj.setdefault("traffic_priority", priorities.get(ap_id, "medium"))
        stas = obj.get("stas")
        if not isinstance(stas, list):
            stas = getattr(self, "_sta_feedback_by_ap", {}).get(ap_id) or []
        if stas:
            obj["stas"] = stas
            feedback = summarize_ap_feedback(ap_id, obj)
            obj["sta_feedback_summary"] = {
                key: value for key, value in feedback.items() if key != "stas"
            }
            obj["sla_violations"] = feedback.get("violations") or []
        payload = {k: obj.get(k) for k in TELEMETRY_KEYS}
        payload["ap_id"] = ap_id
        payload["timestamp"] = datetime.now(timezone.utc).isoformat()
        payload["source"] = "ns3"
        try:
            resp = self._http.post(f"{self.state_server}/state", json=payload, timeout=2)
            status_code = int(getattr(resp, "status_code", 200))
            if status_code != 200:
                body = str(getattr(resp, "text", ""))
                self._last_post_error = {
                    "ap_id": ap_id,
                    "status_code": status_code,
                    "body": body[:500],
                    "at": payload["timestamp"],
                }
                print(
                    f"[bridge] 上报 {ap_id} 被 state server 拒绝: "
                    f"HTTP {status_code} {body[:200]}"
                )
                return
        except requests.RequestException as exc:
            self._last_post_error = {
                "ap_id": ap_id,
                "error": str(exc),
                "at": payload["timestamp"],
            }
            print(f"[bridge] 上报 {ap_id} 失败: {exc}")
            return
        if not hasattr(self, "_last_raw_by_ap"):
            self._last_raw_by_ap = {}
        if not hasattr(self, "_last_payload_by_ap"):
            self._last_payload_by_ap = {}
        if not hasattr(self, "_telemetry_seq"):
            self._telemetry_seq = {}
        cond = getattr(self, "_telemetry_cond", None)
        if cond is None:
            self._last_post_error = None
            self._last_raw_by_ap[ap_id] = raw_obj
            self._last_payload_by_ap[ap_id] = payload
            self._last_telemetry_at = payload["timestamp"]
            self._telemetry_seq[ap_id] = self._telemetry_seq.get(ap_id, 0) + 1
            return
        with cond:
            self._last_post_error = None
            self._last_raw_by_ap[ap_id] = raw_obj
            self._last_payload_by_ap[ap_id] = payload
            self._last_telemetry_at = payload["timestamp"]
            self._telemetry_seq[ap_id] = self._telemetry_seq.get(ap_id, 0) + 1
            cond.notify_all()

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
                start_seq = getattr(self, "_telemetry_seq", {}).get(ap_id, 0)
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
        ack = self._wait_apply_visible(ap_id, details, start_seq)
        result = {"ok": ack["ok"], "command": cmd.strip(), "details": details, "ack": ack}
        if not ack["ok"]:
            result["error"] = ack["error"]
            result["http_status"] = 504
        self._last_apply = {
            **result,
            "ap_id": ap_id,
            "strategy": strategy,
            "at": datetime.now(timezone.utc).isoformat(),
        }
        return result

    def _wait_apply_visible(self, ap_id: str, details: dict, start_seq: int) -> dict:
        timeout = float(getattr(self, "_apply_ack_timeout", 0.0) or 0.0)
        if timeout <= 0:
            return {"ok": True, "mode": "not_waited"}
        deadline = time.monotonic() + timeout
        checked_seq = start_seq
        last_raw: dict | None = None
        last_mismatches: list[dict] = []
        with self._telemetry_cond:
            while time.monotonic() < deadline:
                current_seq = self._telemetry_seq.get(ap_id, 0)
                if current_seq > checked_seq:
                    checked_seq = current_seq
                    raw = self._last_raw_by_ap.get(ap_id) or {}
                    matched, mismatches = _matches_apply_details(raw, details)
                    if matched:
                        return {"ok": True, "mode": "telemetry", "observed": raw}
                    last_raw = raw
                    last_mismatches = mismatches
                remaining = deadline - time.monotonic()
                self._telemetry_cond.wait(timeout=max(0.0, remaining))
        result = {
            "ok": False,
            "mode": "timeout",
            "error": f"timed out waiting for {ap_id} telemetry after APPLY",
        }
        if last_raw is not None:
            result["mismatches"] = last_mismatches
            result["observed"] = last_raw
        return result


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
    if _bridge is None:
        return jsonify({"ok": False, "ns3_alive": False, "error": "bridge is not initialized"}), 503
    alive = _bridge.proc.poll() is None
    last_post_error = getattr(_bridge, "_last_post_error", None)
    body = {
        "ok": bool(alive and last_post_error is None),
        "ns3_alive": alive,
        "last_telemetry_at": getattr(_bridge, "_last_telemetry_at", None),
        "last_post_error": last_post_error,
        "last_apply": getattr(_bridge, "_last_apply", None),
    }
    return jsonify(body), 200 if alive else 503


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
    parser.add_argument("--priorities", default="",
                        help="覆盖业务优先级，格式 ap1=low,ap2=high,ap3=low")
    parser.add_argument("--service-names", default="",
                        help="覆盖业务名称，格式 ap1=background_download,ap2=live_streaming")
    parser.add_argument("--business-types", default="",
                        help="覆盖中文业务类型，格式 ap1=后台下载,ap2=直播")
    parser.add_argument("--apply-ack-timeout", type=float, default=3.0,
                        help="APPLY 后等待下一帧 telemetry 确认生效的秒数；0 表示不等待")
    args = parser.parse_args()
    try:
        priorities = _parse_ap_value_map(
            args.priorities,
            defaults=DEFAULT_PRIORITIES,
            allowed_values={"high", "medium", "low"},
        )
        service_names = _parse_ap_value_map(args.service_names, defaults=DEFAULT_SERVICE_NAMES)
        business_types = _parse_ap_value_map(args.business_types, defaults=DEFAULT_BUSINESS_TYPES)
    except argparse.ArgumentTypeError as exc:
        parser.error(str(exc))

    global _bridge
    _bridge = Ns3Bridge(args.ns3_dir, args.state_server, args.sim_time,
                        args.report_interval, args.scenario, args.ns3_args,
                        priorities=priorities,
                        service_names=service_names,
                        business_types=business_types,
                        apply_ack_timeout=args.apply_ack_timeout)
    _bridge.start_reader()

    print(f"[bridge] /apply 监听 http://0.0.0.0:{args.port} —— 请把 config/ap_endpoints.json "
          f"三个 AP 都指向 http://localhost:{args.port}")
    app.run(host="0.0.0.0", port=args.port, debug=False)


if __name__ == "__main__":
    main()
