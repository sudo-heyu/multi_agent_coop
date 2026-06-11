"""
全局状态服务器 — 运行在 DGX Spark

香蕉派 AP 通过 POST /state 定期上报自身指标；
AP agent 通过 GET /state 获取所有 AP 的最新状态。
"""
import threading
import argparse
import json
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from flask import Flask, request, jsonify, render_template_string

app = Flask(__name__)

VALID_AP_IDS = {"ap1", "ap2", "ap3"}
STALE_THRESHOLD_SECONDS = 60
HISTORY_MAXLEN = 120  # 保留最近 120 条，约 20 分钟（10s/条）
GENERATED_SOURCES = {"mock", "generated", "synthetic", "simulated", "simulation", "random"}
ALLOW_MOCK_SOURCE = False

# 内存存储：{ap_id: {"data": {...}, "timestamp": datetime}}
_store: dict = {}
_lock = threading.Lock()

# 历史缓冲：{ap_id: deque([{"t":..., 指标字段与 MAC 参数字段...}])}
_history: dict = {ap: deque(maxlen=HISTORY_MAXLEN) for ap in ["ap1", "ap2", "ap3"]}
STATE_LOG_DIR = Path("logs") / "state"
_trace_fh = None
_trace_path: Path | None = None
_trace_session_id: str | None = None

REQUIRED_FIELDS = {
    "ap_id", "timestamp",
    "tx_power_dbm", "cwmin", "cwmax", "aifsn",
    "Data_rate_to_bandwidth_ratio", "tx_retries_ratio",
    "neighbor_rssi_dbm", "sta_rssi_dbm", "noise_floor_dbm",
    "throughput_mbps_iperf", "throughput_mbps_user", "ac_iperf", "ac_user",
    "latency_ms", "packet_loss_pct",
}


def _throughput_total(data: dict):
    """iperf + user 吞吐之和；任一缺失则返回可得的一项，全缺返回 None。"""
    vals = [data.get("throughput_mbps_iperf"), data.get("throughput_mbps_user")]
    present = [v for v in vals if isinstance(v, (int, float))]
    return round(sum(present), 2) if present else None


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _utc_stamp() -> str:
    return _now().strftime("%Y%m%d_%H%M%S")


def _age_and_stale(ts: datetime) -> tuple[float, bool]:
    age = (_now() - ts).total_seconds()
    return round(age, 2), age > STALE_THRESHOLD_SECONDS


def _write_trace(row: dict) -> None:
    if _trace_fh is None:
        return
    payload = {
        "recorded_at": _now().isoformat(),
        "session_id": _trace_session_id,
        **row,
    }
    _trace_fh.write(json.dumps(payload, ensure_ascii=False) + "\n")
    _trace_fh.flush()


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
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 16px;
      margin-top: 8px;
    }
    .chart-box {
      background: #fff;
      border-radius: 6px;
      padding: 16px 16px 12px;
      box-shadow: 0 1px 4px rgba(0,0,0,.1);
    }
    .chart-box h3 { margin: 0 0 10px; font-size: 14px; color: #333; }
    canvas { width: 100% !important; height: 170px !important; }
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
      <th>业务优先级</th>
      <th>TX Power(dBm)</th><th>CWmin</th><th>CWmax</th><th>AIFSN</th>
      <th>信道利用率</th><th>重传率</th><th>STA RSSI(dBm)</th><th>噪声底(dBm)</th>
      <th>吞吐iperf(Mbps)</th><th>吞吐user(Mbps)</th><th>AC(i/u)</th><th>延迟(ms)</th><th>丢包(%)</th>
      <th>邻居 RSSI(dBm)</th><th>最后上报</th>
    </tr>
    {% for ap_id in ap_ids %}
    {% set e = rows[ap_id] %}
    <tr>
      <td><b>{{ ap_id.upper() }}</b></td>
      {% if e.data %}
      <td class="{{ 'stale' if e.stale else 'ok' }}">{{ 'STALE' if e.stale else 'OK' }}</td>
      <td>{{ e.age_seconds }}</td>
      {% set prio = e.data.get('traffic_priority', '') %}
      {% if prio == 'high' %}
        <td style="color:#e74c3c;font-weight:bold">high ▲</td>
      {% elif prio == 'low' %}
        <td style="color:#3498db;font-weight:bold">low ▼</td>
      {% elif prio == 'medium' %}
        <td style="color:#f39c12">medium</td>
      {% else %}
        <td class="na">—</td>
      {% endif %}
      <td>{{ e.data.tx_power_dbm }}</td>
      <td>{{ e.data.cwmin }}</td><td>{{ e.data.cwmax }}</td><td>{{ e.data.aifsn }}</td>
      <td>{{ "%.0f%%"|format(e.data.Data_rate_to_bandwidth_ratio * 100) if e.data.Data_rate_to_bandwidth_ratio is not none else "—" }}</td>
      <td>{{ "%.0f%%"|format(e.data.tx_retries_ratio * 100) if e.data.tx_retries_ratio is not none else "—" }}</td>
      <td>{{ e.data.sta_rssi_dbm if e.data.sta_rssi_dbm is not none else "—" }}</td>
      <td>{{ e.data.noise_floor_dbm }}</td>
      <td>{{ e.data.throughput_mbps_iperf }}</td>
      <td>{{ e.data.throughput_mbps_user }}</td>
      <td>{{ e.data.get('ac_iperf', '—') }} / {{ e.data.get('ac_user', '—') }}</td>
      <td>{{ e.data.latency_ms }}</td>
      <td>{{ e.data.packet_loss_pct }}</td>
      <td>{{ e.data.neighbor_rssi_dbm }}</td>
      <td style="font-size:11px">{{ e.timestamp[:19] if e.timestamp else '—' }}</td>
      {% else %}
      <td class="stale">NO DATA</td>
      <td class="na" colspan="17">尚未收到上报</td>
      {% endif %}
    </tr>
    {% endfor %}
  </table>

  <!-- 实时曲线图 -->
  <div id="last-update">等待数据...</div>
  <div class="charts-grid">
    <div class="chart-box">
      <h3>吞吐量 iperf (Mbps)</h3>
      <canvas id="chart-throughput-iperf"></canvas>
    </div>
    <div class="chart-box">
      <h3>吞吐量 user (Mbps)</h3>
      <canvas id="chart-throughput-user"></canvas>
    </div>
    <div class="chart-box">
      <h3>吞吐量 总和 (Mbps)</h3>
      <canvas id="chart-throughput-total"></canvas>
    </div>
    <div class="chart-box">
      <h3>延迟 (ms)</h3>
      <canvas id="chart-latency"></canvas>
    </div>
    <div class="chart-box">
      <h3>丢包率 (%)</h3>
      <canvas id="chart-loss"></canvas>
    </div>
    <div class="chart-box">
      <h3>Txpower (dBm)</h3>
      <canvas id="chart-txpower"></canvas>
    </div>
    <div class="chart-box">
      <h3>CWmin</h3>
      <canvas id="chart-cwmin"></canvas>
    </div>
    <div class="chart-box">
      <h3>CWmax</h3>
      <canvas id="chart-cwmax"></canvas>
    </div>
    <div class="chart-box">
      <h3>AIFSN</h3>
      <canvas id="chart-aifsn"></canvas>
    </div>
  </div>

<script>
const DEFAULT_TIME_WINDOW_S = 40;
const AP_COLORS = {
  ap1: { border: "#e74c3c", bg: "rgba(231,76,60,0.1)" },
  ap2: { border: "#3498db", bg: "rgba(52,152,219,0.1)" },
  ap3: { border: "#2ecc71", bg: "rgba(46,204,113,0.1)" },
};
const APS = ["ap1", "ap2", "ap3"];
const CHART_SPECS = [
  { key: "throughput_mbps_iperf", chart: "throughput_iperf", canvas: "chart-throughput-iperf", label: "Mbps" },
  { key: "throughput_mbps_user",  chart: "throughput_user",  canvas: "chart-throughput-user",  label: "Mbps" },
  { key: "throughput_mbps_total", chart: "throughput_total", canvas: "chart-throughput-total", label: "Mbps" },
  { key: "latency_ms",      chart: "latency",    canvas: "chart-latency",    label: "ms" },
  { key: "packet_loss_pct", chart: "loss",       canvas: "chart-loss",       label: "%" },
  { key: "tx_power_dbm",    chart: "txpower",    canvas: "chart-txpower",    label: "dBm" },
  { key: "cwmin",           chart: "cwmin",      canvas: "chart-cwmin",      label: "CWmin" },
  { key: "cwmax",           chart: "cwmax",      canvas: "chart-cwmax",      label: "CWmax" },
  { key: "aifsn",           chart: "aifsn",      canvas: "chart-aifsn",      label: "AIFSN" },
];

function parseTsMs(value) {
  const ms = Date.parse(value || "");
  return Number.isFinite(ms) ? ms : null;
}

function historyStartMs(history) {
  let first = null;
  APS.forEach(ap => {
    (history[ap] || []).forEach(row => {
      const ms = parseTsMs(row.t);
      if (ms !== null && (first === null || ms < first)) first = ms;
    });
  });
  return first;
}

function elapsedSeconds(row, startMs) {
  const ms = parseTsMs(row.t);
  if (ms === null || startMs === null) return null;
  return Math.max(0, (ms - startMs) / 1000);
}

function timelineMaxSeconds(history, startMs) {
  let maxSec = DEFAULT_TIME_WINDOW_S;
  APS.forEach(ap => {
    (history[ap] || []).forEach(row => {
      const sec = elapsedSeconds(row, startMs);
      if (sec !== null && sec > maxSec) maxSec = sec;
    });
  });
  return Math.ceil(maxSec / 10) * 10;
}

function makeDataset(ap, field, history, startMs) {
  const rows = history[ap] || [];
  return {
    label: ap.toUpperCase(),
    data: rows.map(r => ({ x: elapsedSeconds(r, startMs), y: r[field] }))
      .filter(p => p.x !== null && p.y !== null && p.y !== undefined),
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
    data: { datasets: APS.map(ap => makeDataset(ap, field, {}, null)) },
    options: {
      animation: false,
      responsive: true,
      maintainAspectRatio: false,
      plugins: { legend: { position: "top", labels: { font: { size: 11 } } } },
      scales: {
        x: {
          type: "linear",
          min: 0,
          max: DEFAULT_TIME_WINDOW_S,
          title: { display: true, text: "时间 (s)", font: { size: 11 } },
          ticks: {
            stepSize: 5,
            maxTicksLimit: 9,
            font: { size: 10 },
            callback: value => `${value}s`,
          },
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

const charts = Object.fromEntries(
  CHART_SPECS.map(spec => [spec.chart, makeChart(spec.canvas, spec.key, spec.label)])
);

function refresh() {
  fetch("/history")
    .then(r => r.json())
    .then(history => {
      const startMs = historyStartMs(history);
      const maxSeconds = timelineMaxSeconds(history, startMs);
      CHART_SPECS.forEach(spec => {
        const chart = charts[spec.chart];
        APS.forEach((ap, di) => {
          chart.data.datasets[di].data = makeDataset(ap, spec.key, history, startMs).data;
        });
        chart.options.scales.x.max = maxSeconds;
        chart.update();
      });
      document.getElementById("last-update").textContent =
        "最后更新: " + new Date().toLocaleTimeString() + ` · 时间轴 0-${maxSeconds}s`;
    })
    .catch(() => {});
}

refresh();
setInterval(refresh, 5000);
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

    source = str(body.get("source", "ap")).strip().lower()
    if source in GENERATED_SOURCES and not ALLOW_MOCK_SOURCE:
        return jsonify({
            "error": (
                f"generated data source {source!r} is not accepted by this server; "
                "start with --allow-mock only for local mock tests"
            )
        }), 400

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
            "throughput_mbps_iperf": data.get("throughput_mbps_iperf"),
            "throughput_mbps_user":  data.get("throughput_mbps_user"),
            "throughput_mbps_total": _throughput_total(data),
            "latency_ms":      data.get("latency_ms"),
            "packet_loss_pct": data.get("packet_loss_pct"),
            "tx_power_dbm":    data.get("tx_power_dbm"),
            "cwmin":           data.get("cwmin"),
            "cwmax":           data.get("cwmax"),
            "aifsn":           data.get("aifsn"),
        })
        _write_trace({
            "event": "state_post",
            "ap_id": ap_id,
            "timestamp": ts.isoformat(),
            "parameters": {
                "tx_power_dbm": data.get("tx_power_dbm"),
                "cwmin": data.get("cwmin"),
                "cwmax": data.get("cwmax"),
                "aifsn": data.get("aifsn"),
            },
            "data": data,
        })

    return jsonify({"ok": True, "ap_id": ap_id}), 200


@app.route("/trace/start", methods=["POST"])
def start_trace():
    body = request.get_json(force=True, silent=True) or {}
    session_id = str(body.get("session_id") or _utc_stamp())
    trace_dir = Path(body.get("dir") or STATE_LOG_DIR)
    trace_dir.mkdir(exist_ok=True)
    path = trace_dir / f"telemetry_trace_{_utc_stamp()}_{session_id}.jsonl"

    global _trace_fh, _trace_path, _trace_session_id
    with _lock:
        if _trace_fh is not None:
            _write_trace({"event": "trace_stop", "reason": "replaced"})
            _trace_fh.close()
        _trace_session_id = session_id
        _trace_path = path
        _trace_fh = open(path, "w", encoding="utf-8")
        _write_trace({"event": "trace_start"})

    return jsonify({"ok": True, "path": str(path), "session_id": session_id}), 200


@app.route("/trace/stop", methods=["POST"])
def stop_trace():
    global _trace_fh, _trace_path, _trace_session_id
    with _lock:
        path = str(_trace_path) if _trace_path is not None else None
        if _trace_fh is not None:
            _write_trace({"event": "trace_stop"})
            _trace_fh.close()
        _trace_fh = None
        _trace_path = None
        _trace_session_id = None

    return jsonify({"ok": True, "path": path}), 200


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
    return jsonify({
        "ok": True,
        "reported": ap_status,
        "allow_mock_source": ALLOW_MOCK_SOURCE,
    }), 200


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="全局 AP 状态服务器")
    parser.add_argument(
        "--allow-mock",
        action="store_true",
        help="仅本地联调使用：允许接收 source=mock/generated 的生成数据",
    )
    args = parser.parse_args()
    ALLOW_MOCK_SOURCE = args.allow_mock
    mode = "允许 mock 上报" if ALLOW_MOCK_SOURCE else "真实上报模式（拒收生成数据）"
    print(f"状态服务器启动于 http://0.0.0.0:5001，{mode}")
    app.run(host="0.0.0.0", port=5001, debug=False)
