"""
AP 执行服务 — 运行在香蕉派上

DGX Spark 协商完成后，通过 POST /apply 主动推送最终决策；
本服务接收决策、应用参数、返回执行结果。

用法：
  # mock 模式（本地测试，不执行真实 iw 命令）
  python state_server/executor.py --ap-id ap1 --mock

  # 真实模式（香蕉派上运行）
  python state_server/executor.py --ap-id ap1 --iface wlan0

  # 指定监听端口（默认 5002）
  python state_server/executor.py --ap-id ap1 --port 5002
"""
import argparse
import subprocess
from datetime import datetime, timezone

from flask import Flask, request, jsonify

app = Flask(__name__)

# 由命令行参数填充
_AP_ID: str = ""
_IFACE: str = "wlan0"
_MOCK:  bool = False

# 最近一次执行记录，供 GET /status 查询
_last_result: dict = {}


# ------------------------------------------------------------------
# 参数应用
# ------------------------------------------------------------------

def _run(cmd: str) -> tuple[bool, str]:
    """执行 shell 命令，返回 (success, output/error)。"""
    try:
        out = subprocess.check_output(
            cmd, shell=True, text=True, stderr=subprocess.STDOUT, timeout=10
        )
        return True, out.strip()
    except subprocess.CalledProcessError as e:
        return False, e.output.strip()
    except Exception as e:
        return False, str(e)


def _apply_tx_power(iface: str, dbm: float) -> tuple[bool, str]:
    """
    设置发射功率。
    iw 使用 mBm（毫分贝·毫瓦），1 dBm = 100 mBm。
    需要先将接口设置为允许手动功率控制（regulatory domain 需支持）。
    """
    mbm = int(dbm * 100)
    ok, out = _run(f"iw dev {iface} set txpower fixed {mbm}")
    return ok, out


def _apply_edca(iface: str, cwmin: int, cwmax: int, aifsn: int) -> tuple[bool, str]:
    """
    设置 EDCA 参数（Best Effort 队列）。
    hostapd_cli set_edca_params 格式：<queue> <aifs> <cwmin> <cwmax> <txop>
    queue: BE=0, BK=1, VI=2, VO=3
    """
    ok, out = _run(
        f"hostapd_cli -i {iface} set_edca_params 0 {aifsn} {cwmin} {cwmax} 0"
    )
    return ok, out


def apply_params(strategy: str, params: dict, iface: str, mock: bool) -> dict:
    """
    根据协商策略应用对应参数，返回执行结果摘要。
    """
    results: dict[str, dict] = {}

    if strategy in ("co_sr", "joint"):
        dbm = params.get("tx_power_dbm")
        if dbm is None:
            results["tx_power"] = {"ok": False, "error": "params 中缺少 tx_power_dbm"}
        elif mock:
            print(f"[mock] iw dev {iface} set txpower fixed {int(dbm * 100)} (={dbm} dBm)")
            results["tx_power"] = {"ok": True, "value_dbm": dbm, "mock": True}
        else:
            ok, out = _apply_tx_power(iface, dbm)
            results["tx_power"] = {"ok": ok, "value_dbm": dbm, "output": out}

    if strategy in ("co_edca", "joint"):
        cwmin = params.get("CWmin") or params.get("cwmin")
        cwmax = params.get("CWmax") or params.get("cwmax")
        aifsn = params.get("AIFSN") or params.get("aifsn")
        if any(v is None for v in (cwmin, cwmax, aifsn)):
            results["edca"] = {"ok": False, "error": "params 中缺少 CWmin/CWmax/AIFSN"}
        elif mock:
            print(f"[mock] hostapd_cli set_edca_params 0 {aifsn} {cwmin} {cwmax} 0")
            results["edca"] = {"ok": True, "CWmin": cwmin, "CWmax": cwmax, "AIFSN": aifsn, "mock": True}
        else:
            ok, out = _apply_edca(iface, int(cwmin), int(cwmax), int(aifsn))
            results["edca"] = {"ok": ok, "CWmin": cwmin, "CWmax": cwmax, "AIFSN": aifsn, "output": out}

    overall_ok = all(v.get("ok", False) for v in results.values()) if results else False
    return {"ok": overall_ok, "details": results}


# ------------------------------------------------------------------
# HTTP 端点
# ------------------------------------------------------------------

@app.route("/apply", methods=["POST"])
def apply():
    body = request.get_json(silent=True)
    if not body:
        return jsonify({"ok": False, "error": "body must be JSON"}), 400

    ap_id    = body.get("ap_id", "")
    strategy = body.get("strategy", "")
    params   = body.get("params", {})
    session_id = body.get("session_id", "")

    if ap_id != _AP_ID:
        return jsonify({"ok": False, "error": f"ap_id mismatch: got {ap_id!r}, expected {_AP_ID!r}"}), 400
    if not strategy:
        return jsonify({"ok": False, "error": "missing strategy"}), 400
    if not params:
        return jsonify({"ok": False, "error": "missing params"}), 400

    print(f"\n[executor] 收到决策推送 session={session_id} strategy={strategy}")
    print(f"params: {params}")

    result = apply_params(strategy, params, _IFACE, _MOCK)
    result["ap_id"]      = _AP_ID
    result["session_id"] = session_id
    result["applied_at"] = datetime.now(timezone.utc).isoformat()

    global _last_result
    _last_result = result

    status = "ok" if result["ok"] else "error"
    print(f"[executor] 执行{'成功' if result['ok'] else '失败'} — {result['details']}")
    return jsonify(result), 200 if result["ok"] else 500


@app.route("/status", methods=["GET"])
def status():
    if not _last_result:
        return jsonify({"ap_id": _AP_ID, "status": "idle"}), 200
    return jsonify(_last_result), 200


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"ok": True, "ap_id": _AP_ID, "iface": _IFACE, "mock": _MOCK}), 200


# ------------------------------------------------------------------
# 入口
# ------------------------------------------------------------------

def main():
    global _AP_ID, _IFACE, _MOCK

    parser = argparse.ArgumentParser(description="AP 参数执行服务（香蕉派侧）")
    parser.add_argument("--ap-id", required=True, choices=["ap1", "ap2", "ap3"],
                        help="本机 AP 编号")
    parser.add_argument("--iface", default="wlan0",
                        help="无线接口名称（默认 wlan0）")
    parser.add_argument("--port", type=int, default=5002,
                        help="监听端口（默认 5002）")
    parser.add_argument("--mock", action="store_true",
                        help="mock 模式：打印命令但不实际执行")
    args = parser.parse_args()

    _AP_ID = args.ap_id
    _IFACE = args.iface
    _MOCK  = args.mock

    mode = "mock" if _MOCK else f"iface={_IFACE}"
    print(f"AP 执行服务启动 — ap_id={_AP_ID}  port={args.port}  {mode}")
    app.run(host="0.0.0.0", port=args.port, debug=False)


if __name__ == "__main__":
    main()
