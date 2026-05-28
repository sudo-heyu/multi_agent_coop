"""
全局状态服务器 — 运行在 DGX Spark

香蕉派 AP 通过 POST /state 定期上报自身指标；
AP agent 通过 GET /state 获取所有 AP 的最新状态。
"""
import threading
from collections import deque
from datetime import datetime, timezone
from flask import Flask, request, jsonify, render_template_string

app = Flask(__name__)

VALID_AP_IDS = {"ap1", "ap2", "ap3"}
STALE_THRESHOLD_SECONDS = 60
HISTORY_MAXLEN = 120  # 保留最近 120 条，约 20 分钟（10s/条）

# 内存存储：{ap_id: {"data": {...}, "timestamp": datetime}}
_store: dict = {}
_lock = threading.Lock()

# 历史缓冲：{ap_id: deque([{"t":..., "throughput_mbps":..., "latency_ms":..., "packet_loss_pct":...}])}
_history: dict = {ap: deque(maxlen=HISTORY_MAXLEN) for ap in ["ap1", "ap2", "ap3"]}

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
  <title>AP 状态监控</title>
  <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.3/dist/chart.umd.min.js"></script>
  <style>
    body  { font-family: monospace; padding: 24px; background: #f5f5f5; margin: 0; }
    h2    { margin-bottom: 4px; }
    p.sub { color: #666; margin: 0 0 20px; font-size: 13px; }
    table { border-collapse: collapse; width: 100%; background: #fff;
            box-shadow: 0 1px 4px rgba(0,0,0,.1); margin-bottom: 32px; }
    th    { background: #333; color: #fff; padding: 8px 12px; text-align: left; }
    td    { padding: 7px 12px; border-bottom: 1px solid #eee; }
    tr:hover td { background: #f0f7ff; }
    .ok   { color: #27ae60; font-weight: bold; }
    .stale{ color: #e74c3c; font-weight: bold; }
    .na   { color: #aaa; }
    .charts-grid {
      display: grid;
      grid-template-columns: repeat(3, 1fr);
      gap: 20px;
      margin-top: 8px;
    }
    .chart-box {
      background: #fff;
      border-radius: 6px;
      padding: 16px 16px 12px;
      box-shadow: 0 1px 4px rgba(0,0,0,.1);
    }
    .chart-box h3 { margin: 0 0 10px; font-size: 14px; color: #333; }
    canvas { width: 100% !important; height: 200px !important; }
    #last-update { color: #888; font-size: 12px; margin-bottom: 12px; }
  </style>
</head>
<body>
  <h2>AP 全局状态</h2>
  <p class="sub">图表每 5 秒自动刷新 &nbsp;|&nbsp; 数据超过 {{ stale_sec }}s 未更新标记为 STALE</p>

  <!-- 状态表格 -->
  <table>
    <tr>
      <th>AP</th><th>状态</th><th>数据age(s)</th>
      <th>TX Power(dBm)</th><th>CWmin</th><th>CWmax</th><th>AIFSN</th>
      <th>信道占用</th><th>重传率</th><th>STA RSSI(dBm)</th><th>噪声底(dBm)</th>
      <th>吞吐量(Mbps)</th><th>延迟(ms)</th><th>丢包(%)</th>
      <th>邻居 RSSI(dBm)</th><th>最后上报</th>
    </tr>
    {% for ap_id in ap_ids %}
    {% set e = rows[ap_id] %}
    <tr>
      <td><b>{{ ap_id.upper() }}</b></td>
      {% if e.data %}
      <td class="{{ 'stale' if e.stale else 'ok' }}">{{ 'STALE' if e.stale else 'OK' }}</td>
      <td>{{ e.age_seconds }}</td>
      <td>{{ e.data.tx_power_dbm }}</td>
      <td>{{ e.data.cwmin }}</td><td>{{ e.data.cwmax }}</td><td>{{ e.data.aifsn }}</td>
      <td>{{ "%.0f%%"|format(e.data.channel_busy_ratio * 100) if e.data.channel_busy_ratio is not none else "—" }}</td>
      <td>{{ "%.0f%%"|format(e.data.tx_retries_ratio * 100) if e.data.tx_retries_ratio is not none else "—" }}</td>
      <td>{{ e.data.sta_rssi_dbm if e.data.sta_rssi_dbm is not none else "—" }}</td>
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

  <!-- 实时曲线图 -->
  <div id="last-update">等待数据...</div>
  <div class="charts-grid">
    <div class="chart-box">
      <h3>吞吐量 (Mbps)</h3>
      <canvas id="chart-throughput"></canvas>
    </div>
    <div class="chart-box">
      <h3>延迟 (ms)</h3>
      <canvas id="chart-latency"></canvas>
    </div>
    <div class="chart-box">
      <h3>丢包率 (%)</h3>
      <canvas id="chart-loss"></canvas>
    </div>
  </div>

<script>
const AP_COLORS = {
  ap1: { border: "#e74c3c", bg: "rgba(231,76,60,0.1)" },
  ap2: { border: "#3498db", bg: "rgba(52,152,219,0.1)" },
  ap3: { border: "#2ecc71", bg: "rgba(46,204,113,0.1)" },
};
const APS = ["ap1", "ap2", "ap3"];

function makeDataset(ap, field, history) {
  const rows = history[ap] || [];
  return {
    label: ap.toUpperCase(),
    data: rows.map(r => ({ x: r.t, y: r[field] })),
    borderColor: AP_COLORS[ap].border,
    backgroundColor: AP_COLORS[ap].bg,
    borderWidth: 2,
    pointRadius: 2,
    tension: 0.3,
    fill: false,
  };
}

function makeChart(canvasId, field, yLabel) {
  const ctx = document.getElementById(canvasId).getContext("2d");
  return new Chart(ctx, {
    type: "line",
    data: { datasets: APS.map(ap => makeDataset(ap, field, {})) },
    options: {
      animation: false,
      responsive: true,
      maintainAspectRatio: false,
      plugins: { legend: { position: "top", labels: { font: { size: 11 } } } },
      scales: {
        x: {
          type: "time",
          time: { unit: "minute", displayFormats: { minute: "HH:mm", second: "HH:mm:ss" } },
          ticks: { maxTicksLimit: 6, font: { size: 10 } },
        },
        y: {
          title: { display: true, text: yLabel, font: { size: 11 } },
          ticks: { font: { size: 10 } },
          beginAtZero: true,
        },
      },
    },
  });
}

// 需要 Chart.js 时间轴适配器
const script = document.createElement("script");
script.src = "https://cdn.jsdelivr.net/npm/chartjs-adapter-date-fns@3.0.0/dist/chartjs-adapter-date-fns.bundle.min.js";
script.onload = () => {
  const charts = {
    throughput: makeChart("chart-throughput", "throughput_mbps", "Mbps"),
    latency:    makeChart("chart-latency",    "latency_ms",      "ms"),
    loss:       makeChart("chart-loss",       "packet_loss_pct", "%"),
  };

  function refresh() {
    fetch("/history")
      .then(r => r.json())
      .then(history => {
        const fields = ["throughput_mbps", "latency_ms", "packet_loss_pct"];
        const keys   = ["throughput", "latency", "loss"];
        keys.forEach((key, i) => {
          const chart = charts[key];
          APS.forEach((ap, di) => {
            const rows = history[ap] || [];
            chart.data.datasets[di].data = rows.map(r => ({ x: r.t, y: r[fields[i]] }));
          });
          chart.update();
        });
        document.getElementById("last-update").textContent =
          "最后更新: " + new Date().toLocaleTimeString();
      })
      .catch(() => {});
  }

  refresh();
  setInterval(refresh, 5000);
};
document.head.appendChild(script);
</script>
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
        _history[ap_id].append({
            "t": ts.isoformat(),
            "throughput_mbps": data.get("throughput_mbps"),
            "latency_ms":      data.get("latency_ms"),
            "packet_loss_pct": data.get("packet_loss_pct"),
        })

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
# GET /history  —  返回各 AP 历史时序数据（供图表使用）
# ------------------------------------------------------------------
@app.route("/history", methods=["GET"])
def get_history():
    with _lock:
        result = {ap: list(_history[ap]) for ap in ["ap1", "ap2", "ap3"]}
    return jsonify(result), 200


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
