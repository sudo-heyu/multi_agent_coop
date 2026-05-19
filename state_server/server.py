"""
全局状态服务器 — 运行在 DGX Spark

香蕉派 AP 通过 POST /state 定期上报自身指标；
AP agent 通过 GET /state 获取所有 AP 的最新状态。
"""
import threading
from datetime import datetime, timezone
from flask import Flask, request, jsonify, render_template_string

app = Flask(__name__)

VALID_AP_IDS = {"ap1", "ap2", "ap3"}
STALE_THRESHOLD_SECONDS = 60

# 内存存储：{ap_id: {"data": {...}, "timestamp": datetime}}
_store: dict = {}
_lock = threading.Lock()

REQUIRED_FIELDS = {
    "ap_id", "timestamp",
    "tx_power_dbm", "cwmin", "cwmax", "aifsn",
    "channel_busy_ratio", "tx_retries_ratio",
    "neighbor_rssi_dbm", "sta_rssi_dbm", "noise_floor_dbm",
    "throughput_mbps", "latency_ms", "packet_loss_pct",
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _age_and_stale(ts: datetime) -> tuple[float, bool]:
    age = (_now() - ts).total_seconds()
    return round(age, 2), age > STALE_THRESHOLD_SECONDS


_INDEX_HTML = """
<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <meta http-equiv="refresh" content="10">
  <title>AP 状态监控</title>
  <style>
    body { font-family: monospace; padding: 24px; background: #f5f5f5; }
    h2   { margin-bottom: 4px; }
    p.sub{ color: #666; margin: 0 0 20px; font-size: 13px; }
    table{ border-collapse: collapse; width: 100%; background: #fff;
           box-shadow: 0 1px 4px rgba(0,0,0,.1); }
    th   { background: #333; color: #fff; padding: 8px 12px; text-align: left; }
    td   { padding: 7px 12px; border-bottom: 1px solid #eee; }
    tr:hover td { background: #f0f7ff; }
    .ok  { color: #27ae60; font-weight: bold; }
    .stale{ color: #e74c3c; font-weight: bold; }
    .na  { color: #aaa; }
  </style>
</head>
<body>
  <h2>AP 全局状态</h2>
  <p class="sub">每 10 秒自动刷新 &nbsp;|&nbsp; 数据超过 {{ stale_sec }}s 未更新标记为 STALE</p>
  <table>
    <tr>
      <th>AP</th>
      <th>状态</th>
      <th>数据age(s)</th>
      <th>TX Power(dBm)</th>
      <th>CWmin</th><th>CWmax</th><th>AIFSN</th>
      <th>信道占用</th>
      <th>重传率</th>
      <th>STA RSSI(dBm)</th>
      <th>噪声底(dBm)</th>
      <th>吞吐量(Mbps)</th>
      <th>延迟(ms)</th>
      <th>丢包(%)</th>
      <th>邻居 RSSI(dBm)</th>
      <th>最后上报</th>
    </tr>
    {% for ap_id in ap_ids %}
    {% set e = rows[ap_id] %}
    <tr>
      <td><b>{{ ap_id.upper() }}</b></td>
      {% if e.data %}
      <td class="{{ 'stale' if e.stale else 'ok' }}">
        {{ 'STALE' if e.stale else 'OK' }}
      </td>
      <td>{{ e.age_seconds }}</td>
      <td>{{ e.data.tx_power_dbm }}</td>
      <td>{{ e.data.cwmin }}</td>
      <td>{{ e.data.cwmax }}</td>
      <td>{{ e.data.aifsn }}</td>
      <td>{{ "%.0f%%"|format(e.data.channel_busy_ratio * 100) }}</td>
      <td>{{ "%.0f%%"|format(e.data.tx_retries_ratio * 100) }}</td>
      <td>{{ e.data.sta_rssi_dbm }}</td>
      <td>{{ e.data.noise_floor_dbm }}</td>
      <td>{{ e.data.throughput_mbps }}</td>
      <td>{{ e.data.latency_ms }}</td>
      <td>{{ e.data.packet_loss_pct }}</td>
      <td>{{ e.data.neighbor_rssi_dbm }}</td>
      <td style="font-size:11px">{{ e.timestamp[:19] if e.timestamp else '—' }}</td>
      {% else %}
      <td class="stale">NO DATA</td>
      <td class="na" colspan="14">尚未收到上报</td>
      {% endif %}
    </tr>
    {% endfor %}
  </table>
</body>
</html>
"""


# ------------------------------------------------------------------
# GET /  —  状态监控页面
# ------------------------------------------------------------------
@app.route("/", methods=["GET"])
def index():
    with _lock:
        snapshot = dict(_store)

    rows = {}
    for ap_id in sorted(VALID_AP_IDS):
        if ap_id not in snapshot:
            rows[ap_id] = {"data": None, "timestamp": None,
                           "age_seconds": None, "stale": True}
        else:
            entry = snapshot[ap_id]
            age, stale = _age_and_stale(entry["timestamp"])
            rows[ap_id] = {
                "data": entry["data"],
                "timestamp": entry["timestamp"].isoformat(),
                "age_seconds": age,
                "stale": stale,
            }

    return render_template_string(
        _INDEX_HTML,
        ap_ids=sorted(VALID_AP_IDS),
        rows=rows,
        stale_sec=STALE_THRESHOLD_SECONDS,
    )


# ------------------------------------------------------------------
# POST /state  —  AP 上报自身指标
# ------------------------------------------------------------------
@app.route("/state", methods=["POST"])
def post_state():
    body = request.get_json(silent=True)
    if not body:
        return jsonify({"error": "body must be JSON"}), 400

    missing = REQUIRED_FIELDS - body.keys()
    if missing:
        return jsonify({"error": f"missing fields: {sorted(missing)}"}), 400

    ap_id = body.get("ap_id", "")
    if ap_id not in VALID_AP_IDS:
        return jsonify({"error": f"unknown ap_id: {ap_id!r}"}), 400

    try:
        ts = datetime.fromisoformat(body["timestamp"])
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
    except ValueError:
        return jsonify({"error": "timestamp must be ISO 8601"}), 400

    data = {k: v for k, v in body.items() if k not in ("ap_id", "timestamp")}

    with _lock:
        _store[ap_id] = {"data": data, "timestamp": ts}

    return jsonify({"ok": True, "ap_id": ap_id}), 200


# ------------------------------------------------------------------
# GET /state  —  返回所有 AP 最新状态
# ------------------------------------------------------------------
@app.route("/state", methods=["GET"])
def get_all_state():
    with _lock:
        snapshot = dict(_store)

    result = {}
    for ap_id in VALID_AP_IDS:
        if ap_id not in snapshot:
            result[ap_id] = {"data": None, "timestamp": None,
                             "age_seconds": None, "stale": True}
        else:
            entry = snapshot[ap_id]
            age, stale = _age_and_stale(entry["timestamp"])
            result[ap_id] = {
                "data": entry["data"],
                "timestamp": entry["timestamp"].isoformat(),
                "age_seconds": age,
                "stale": stale,
            }
    return jsonify(result), 200


# ------------------------------------------------------------------
# GET /state/<ap_id>  —  返回单个 AP 状态
# ------------------------------------------------------------------
@app.route("/state/<ap_id>", methods=["GET"])
def get_one_state(ap_id):
    if ap_id not in VALID_AP_IDS:
        return jsonify({"error": f"unknown ap_id: {ap_id!r}"}), 400

    with _lock:
        entry = _store.get(ap_id)

    if entry is None:
        return jsonify({"data": None, "timestamp": None,
                        "age_seconds": None, "stale": True}), 200

    age, stale = _age_and_stale(entry["timestamp"])
    return jsonify({
        "data": entry["data"],
        "timestamp": entry["timestamp"].isoformat(),
        "age_seconds": age,
        "stale": stale,
    }), 200


# ------------------------------------------------------------------
# GET /health
# ------------------------------------------------------------------
@app.route("/health", methods=["GET"])
def health():
    with _lock:
        ap_status = {
            ap_id: ap_id in _store for ap_id in VALID_AP_IDS
        }
    return jsonify({"ok": True, "reported": ap_status}), 200


if __name__ == "__main__":
    print("状态服务器启动于 http://0.0.0.0:5001")
    app.run(host="0.0.0.0", port=5001, debug=False)
