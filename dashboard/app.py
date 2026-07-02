"""
协商 Dashboard 服务（端口 5050）

左：SSE 实时对话流（tail JSONL 日志）
右：业务指标与 MAC 参数曲线图（轮询 state server /history）

由 run.py 自动启动，也可手动运行：
  python dashboard/app.py [--port 5050] [--state-server http://localhost:5001]
"""
import argparse
import json
import os
import queue as _queue
import sys
import threading
import time
from pathlib import Path

# dashboard 作为独立进程运行时 sys.path[0] 是 dashboard/，补上 repo root 才能 import src.*
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from flask import Flask, Response, request, jsonify, stream_with_context
import requests as _req

app = Flask(__name__)
_STATE_SERVER = "http://localhost:5001"

# ── 实时事件扇出（主进程 orchestrator → 所有 dashboard SSE 连接）─────────────
# 每个 live SSE 连接持有独立队列，push_event 广播给全部订阅者。
# 不能用单一共享队列竞争消费：多开标签页 / 刷新 / EventSource 自动重连会产生
# 多个消费者，竞争 get() 会把 token 事件瓜分到不同连接，导致每个页面掉字。
_subscribers:      set[_queue.SimpleQueue] = set()
_subscribers_lock: threading.Lock          = threading.Lock()
_backlog:          list[dict]              = []        # 本会话已发生事件，供新连接补播
_backlog_lock:     threading.Lock          = threading.Lock()
_client_ready:     threading.Event         = threading.Event()   # 浏览器已连上 SSE 时置位


def push_event(d: dict) -> None:
    """由 orchestrator 线程调用，将事件广播给所有已连接的实时客户端。"""
    with _backlog_lock:
        if d.get("event") == "session_start":
            _backlog.clear()
        _backlog.append(d)
        with _subscribers_lock:
            targets = list(_subscribers)
    for q in targets:
        q.put(d)


def wait_for_client(timeout: float = 30.0) -> bool:
    """
    阻塞直到浏览器 EventSource 建立连接（或超时）。
    返回 True 表示已连接，False 表示超时。
    """
    return _client_ready.wait(timeout)


def start_server_thread(
    port: int = 5050,
    state_server: str = "http://localhost:5001",
) -> None:
    """在后台守护线程中启动 Flask，阻塞直到端口就绪（最多 10s）。"""
    global _STATE_SERVER
    _STATE_SERVER = state_server

    t = threading.Thread(
        target=lambda: app.run(
            host="0.0.0.0",
            port=port,
            debug=False,
            threaded=True,
            use_reloader=False,
        ),
        daemon=True,
        name="dashboard-server",
    )
    t.start()

    deadline = time.time() + 10
    while time.time() < deadline:
        try:
            _req.get(f"http://localhost:{port}/", timeout=0.5)
            return
        except Exception:
            time.sleep(0.25)

# ---------------------------------------------------------------------------
# Dashboard HTML（单页，Chart.js + SSE）
# ---------------------------------------------------------------------------
_HTML = r"""<!doctype html>
<html lang="zh">
<head>
<meta charset="utf-8">
<title>Multi-AP 协商 · 实时监控</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.3/dist/chart.umd.min.js"></script>
<style>
* { box-sizing: border-box; margin: 0; padding: 0; }
body {
  font-family: 'SF Mono', 'Consolas', 'Menlo', monospace;
  background: #111827; color: #d1d5db;
  height: 100vh; display: flex; flex-direction: column; overflow: hidden;
}
/* Header */
header {
  padding: 9px 20px; background: #1f2937;
  border-bottom: 1px solid #374151;
  display: flex; align-items: center; gap: 14px; flex-shrink: 0;
}
header h1 { font-size: 16px; color: #93c5fd; letter-spacing: .5px; }
#badge {
  font-size: 12px; padding: 3px 10px; border-radius: 10px;
  background: #374151; color: #9ca3af;
}
#badge.running { background: #14532d; color: #4ade80; }
#badge.done    { background: #1e1b4b; color: #a5b4fc; }
#badge.error   { background: #450a0a; color: #f87171; }
#session-meta { margin-left: auto; font-size: 12px; color: #6b7280; }

/* Two-column layout */
main { flex: 1; display: grid; grid-template-columns: 1fr 1fr; overflow: hidden; }

/* ── Left: conversation ── */
#conv-panel { border-right: 1px solid #1f2937; display: flex; flex-direction: column; overflow: hidden; }
#conv-hdr { padding: 7px 14px; font-size: 13px; color: #9ca3af; background: #1f2937; border-bottom: 1px solid #374151; flex-shrink: 0; }
#conv-body { flex: 1; overflow-y: auto; padding: 10px 12px; display: flex; flex-direction: column; gap: 7px; }

/* event styles */
.ev-sys   { font-size: 12px; color: #6b7280; text-align: center; padding: 4px 0; }
.ev-phase { border-left: 3px solid #374151; padding: 4px 12px; font-size: 13px; color: #9ca3af; margin: 4px 0; }

.ev-speak { background: #1f2937; border-left: 3px solid #374151; padding: 9px 13px; border-radius: 4px; font-size: 14px; line-height: 1.65; }
.ev-speak .tag  { font-size: 12px; font-weight: bold; margin-bottom: 5px; }
.ev-speak .body { color: #f9fafb; white-space: pre-wrap; word-break: break-word; max-height: 220px; overflow-y: auto; }
.ev-speak .body.typing::after { content: '▋'; animation: blink .7s step-end infinite; opacity: .7; }
@keyframes blink { 50% { opacity: 0; } }
.ev-speak.ap1         .tag { color: #60a5fa; }
.ev-speak.ap2         .tag { color: #fb923c; }
.ev-speak.ap3         .tag { color: #c084fc; }
.ev-speak.coordinator .tag { color: #2dd4bf; }

.votes-row  { display: flex; gap: 6px; flex-wrap: wrap; }
.ev-vote    { font-size: 12px; padding: 3px 9px; border-radius: 3px; }
.ev-vote.ok { background: #14532d; color: #4ade80; }
.ev-vote.no { background: #450a0a; color: #f87171; }
.ev-round   { font-size: 12px; color: #6b7280; border-top: 1px dashed #374151; padding-top: 5px; margin-top: 2px; }

.ev-decision { background: #1f2937; border: 1px solid #374151; border-radius: 5px; padding: 10px 14px; font-size: 13px; }
.ev-decision h4 { color: #4ade80; margin-bottom: 6px; font-size: 13px; }
.ev-decision pre { color: #d1d5db; white-space: pre-wrap; word-break: break-word; max-height: 180px; overflow-y: auto; }

.ev-done       { background: #1e1b4b; border: 1px solid #4338ca; border-radius: 5px; padding: 8px 14px; font-size: 13px; color: #a5b4fc; text-align: center; }
.ev-done.error { background: #450a0a; border-color: #b91c1c; color: #fca5a5; }
.ev-tool       { font-size: 11px; color: #4b5563; padding: 2px 10px; border-left: 2px solid #374151; }

/* ── Right: charts ── */
#charts-panel { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); grid-auto-rows: minmax(112px, 1fr); overflow: hidden; }
.chart-box { display: flex; flex-direction: column; border-bottom: 1px solid #1f2937; border-right: 1px solid #1f2937; padding: 8px 12px 6px; min-width: 0; min-height: 0; }
.chart-box:nth-child(2n) { border-right: none; }
.chart-box h3 { font-size: 12px; color: #9ca3af; margin-bottom: 6px; flex-shrink: 0; }
.chart-box canvas { flex: 1; min-height: 0; }
#metrics-ts { grid-column: 1 / -1; font-size: 11px; color: #4b5563; padding: 4px 16px; min-height: 22px; }
</style>
</head>
<body>
<header>
  <h1>Multi-AP Wi-Fi 协商 · 实时监控</h1>
  <span id="badge">连接中...</span>
  <span id="session-meta"></span>
</header>
<main>
  <div id="conv-panel">
    <div id="conv-hdr">对话过程</div>
    <div id="conv-body"><div class="ev-sys">等待协商开始...</div></div>
  </div>
  <div id="charts-panel">
    <div class="chart-box"><h3>吞吐量 iperf (Mbps)</h3><canvas id="c-tput-iperf"></canvas></div>
    <div class="chart-box"><h3>吞吐量 user (Mbps)</h3><canvas id="c-tput-user"></canvas></div>
    <div class="chart-box"><h3>吞吐量 总和 (Mbps)</h3><canvas id="c-tput-total"></canvas></div>
    <div class="chart-box"><h3>延迟 (ms)</h3><canvas id="c-lat"></canvas></div>
    <div class="chart-box"><h3>丢包率 (%)</h3><canvas id="c-loss"></canvas></div>
    <div class="chart-box"><h3>Txpower (dBm)</h3><canvas id="c-txpower"></canvas></div>
    <div class="chart-box"><h3>CWmin</h3><canvas id="c-cwmin"></canvas></div>
    <div class="chart-box"><h3>CWmax</h3><canvas id="c-cwmax"></canvas></div>
    <div class="chart-box"><h3>AIFSN</h3><canvas id="c-aifsn"></canvas></div>
    <div id="metrics-ts">指标：等待数据...</div>
  </div>
</main>
<script>
// ── Chart.js ────────────────────────────────────────────────────
const DEFAULT_TIME_WINDOW_S = 40;
const AP_COLORS = {
  ap1: { border: "#e74c3c", bg: "rgba(231,76,60,0.1)" },
  ap2: { border: "#3498db", bg: "rgba(52,152,219,0.1)" },
  ap3: { border: "#2ecc71", bg: "rgba(46,204,113,0.1)" },
};
const APS = ["ap1","ap2","ap3"];
let charts = {};
let negotiationStartMs = null;

const CHART_SPECS = [
  { key: "throughput_mbps_iperf", canvas: "c-tput-iperf", label: "Mbps" },
  { key: "throughput_mbps_user",  canvas: "c-tput-user",  label: "Mbps" },
  { key: "throughput_mbps_total", canvas: "c-tput-total", label: "Mbps" },
  { key: "latency_ms",      canvas: "c-lat",     label: "ms" },
  { key: "packet_loss_pct", canvas: "c-loss",    label: "%" },
  { key: "tx_power_dbm",    canvas: "c-txpower", label: "dBm" },
  { key: "cwmin",           canvas: "c-cwmin",   label: "CWmin" },
  { key: "cwmax",           canvas: "c-cwmax",   label: "CWmax" },
  { key: "aifsn",           canvas: "c-aifsn",   label: "AIFSN" },
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

charts = Object.fromEntries(
  CHART_SPECS.map(spec => [spec.key, makeChart(spec.canvas, spec.key, spec.label)])
);

function refreshMetrics() {
  fetch("/metrics").then(r => r.ok ? r.json() : {}).then(hist => {
    const startMs = negotiationStartMs ?? historyStartMs(hist);
    const maxSeconds = timelineMaxSeconds(hist, startMs);
    CHART_SPECS.forEach(spec => {
      const ch = charts[spec.key]; if (!ch) return;
      APS.forEach((ap, i) => {
        ch.data.datasets[i].data = makeDataset(ap, spec.key, hist, startMs).data;
      });
      ch.options.scales.x.max = maxSeconds;
      ch.update();
    });
    document.getElementById("metrics-ts").textContent =
      "指标更新: " + new Date().toLocaleTimeString() + ` · 时间轴 0-${maxSeconds}s`;
  }).catch(() => {});
}
refreshMetrics();
setInterval(refreshMetrics, 10000);

// ── SSE conversation stream ─────────────────────────────────────
const _sp     = new URLSearchParams(location.search);
const logPath = _sp.get("log") || "";
const _isLive = _sp.get("live") === "1";
const _sseP   = new URLSearchParams();
if (_isLive)  _sseP.set("live", "1");
if (logPath)  _sseP.set("log",  logPath);
const sse = new EventSource("/events?" + _sseP.toString());
const body  = document.getElementById("conv-body");
const badge = document.getElementById("badge");
let autoScroll = true;

body.addEventListener("scroll", () => {
  autoScroll = body.scrollTop + body.clientHeight >= body.scrollHeight - 24;
});
function sd() { if (autoScroll) body.scrollTop = body.scrollHeight; }

function mk(cls, html) {
  const el = document.createElement("div");
  if (cls) el.className = cls;
  if (html !== undefined) el.innerHTML = html;
  return el;
}
function esc(s) {
  return String(s).replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;");
}
function app_(el) { body.appendChild(el); sd(); }

// ── 流式发言状态 ────────────────────────────────────────────────
let _activeTurn = null;
let _turnSeq = 0;

// ── 打字机（turn 队列）────────────────────────────────────────────
// turn = {id, agent, el, body, buf, received, done}
const _TYPE_CPS_LIVE   = 140;
const _TYPE_CPS_REPLAY = 260;
const _turns = [];
let _typeRafId = null;
let _typeLastTs = 0;

const _typeCPS = () => _isLive ? _TYPE_CPS_LIVE : _TYPE_CPS_REPLAY;
const _hasBufferedText = () => _turns.some(t => t.buf.length > 0);

function _typeTick(ts) {
  if (!_hasBufferedText()) { _typeRafId = null; return; }
  if (!_typeLastTs) _typeLastTs = ts;
  const elapsed = Math.max(16, ts - _typeLastTs);
  _typeLastTs = ts;

  let budget = Math.max(1, Math.floor(_typeCPS() * elapsed / 1000));
  for (const turn of _turns) {
    if (budget <= 0) break;
    if (!turn.buf.length) continue;
    const take = Math.min(budget, turn.buf.length);
    turn.body.textContent += turn.buf.slice(0, take);
    turn.buf = turn.buf.slice(take);
    budget -= take;
    if (turn.done && !turn.buf.length) turn.body.classList.remove("typing");
    sd();
  }
  _typeRafId = _hasBufferedText() ? requestAnimationFrame(_typeTick) : null;
}

function _typeStartRAF() {
  if (!_typeRafId && _hasBufferedText()) {
    _typeLastTs = 0;
    _typeRafId = requestAnimationFrame(_typeTick);
  }
}

function _typeReset() {
  if (_typeRafId) cancelAnimationFrame(_typeRafId);
  _typeRafId = null;
  _typeLastTs = 0;
  _turns.length = 0;
  _activeTurn = null;
}

function _typeCompleteAll() {
  for (const turn of _turns) {
    turn.done = true;
    if (!turn.buf.length) turn.body.classList.remove("typing");
  }
}

function agentClass(ag) {
  return ["ap1","ap2","ap3"].includes(ag) ? ag : "coordinator";
}
function agentName(ag) {
  return ag === "coordinator" ? "协调者" : ag.toUpperCase();
}

function _startTurn(agent, phase, role) {
  const ag = (agent || "coordinator").toLowerCase();
  if (_activeTurn && _activeTurn.agent === ag && !_activeTurn.done) {
    _activeTurn.buf = "";
    _activeTurn.received = 0;
    _activeTurn.body.textContent = "";
    _activeTurn.body.classList.add("typing");
    return _activeTurn;
  }

  const bubble = mk("ev-speak " + agentClass(ag),
    `<div class="tag">${agentName(ag)}</div><div class="body typing"></div>`);
  const bodyEl = bubble.querySelector(".body");
  const turn = {
    id: ++_turnSeq,
    agent: ag,
    phase: phase || 0,
    role: role || "",
    el: bubble,
    body: bodyEl,
    buf: "",
    received: 0,
    done: false,
  };
  _turns.push(turn);
  _activeTurn = turn;
  app_(bubble);
  return turn;
}

function _appendTurnText(turn, text) {
  const value = String(text || "");
  if (!turn || !value) return;
  turn.buf += value;
  turn.received += value.length;
  turn.body.classList.add("typing");
  _typeStartRAF();
}

function _completeTurn(agent, response) {
  const ag = (agent || "coordinator").toLowerCase();
  let turn = _activeTurn && _activeTurn.agent === ag ? _activeTurn : null;

  if (!turn) {
    turn = _startTurn(ag, 0, "fallback");
  }

  if (turn.received === 0 && response) {
    _appendTurnText(turn, response);
  }

  turn.done = true;
  if (!turn.buf.length) turn.body.classList.remove("typing");
  if (_activeTurn === turn) _activeTurn = null;
}

function render(d) {
  const ev = d.event;

  if (ev === "_wait" || ev === "_error") {
    app_(mk("ev-sys", d.msg || ev)); return;
  }

  if (ev === "session_start") {
    body.innerHTML = "";
    _typeReset();
    negotiationStartMs = parseTsMs(d.ts) ?? Date.now();
    document.getElementById("session-meta").textContent =
      `session=${d.session_id}  model=${d.model}  scene=${d.scene}`;
    badge.textContent = "运行中"; badge.className = "running";
    app_(mk("ev-sys", `会话开始 · 场景 ${(d.scene||"").toUpperCase()} · ${d.model}`));
    return;
  }

  if (ev === "phase_start" || ev === "tool_call") {
    return;
  }

  if (ev === "agent_speak_start") {
    _startTurn(d.agent, d.phase, d.role);
    return;
  }

  if (ev === "agent_speak_chunk") {
    const ag = (d.agent || "coordinator").toLowerCase();
    const turn = (_activeTurn && _activeTurn.agent === ag && !_activeTurn.done)
      ? _activeTurn
      : _startTurn(ag, d.phase, d.role);
    _appendTurnText(turn, d.text || "");
    return;
  }

  if (ev === "agent_speak") {
    _completeTurn(d.agent, d.response || "");
    return;
  }

  if (ev === "vote") {
    const rid = "vr-" + (d.round||0);
    let row = document.getElementById(rid);
    if (!row) { row = mk("votes-row"); row.id = rid; app_(row); }
    const _vok  = d.agreed === true || d.agreed === "agree" || d.agreed === "abstain";
    const _vsym = d.agreed === "abstain" ? "～" : (_vok ? "✓" : "✗");
    row.appendChild(mk("ev-vote " + (_vok ? "ok" : "no"),
      _vsym + " " + (d.voter||"").toUpperCase()));
    sd(); return;
  }

  if (ev === "round_result") {
    const pass = d.all_agreed;
    app_(mk("ev-round",
      `第 ${d.round} 轮：${d.agree_count}/${d.total_voters} 同意 · ` +
      (pass ? `<span style="color:#4ade80">全票通过</span>`
             : `<span style="color:#f87171">未通过，进入修订</span>`)
    )); return;
  }

  if (ev === "final_decision") {
    const txt = d.decision
      ? JSON.stringify(d.decision, null, 2)
      : "(解析失败)\n" + (d.raw_response||"").slice(0,300);
    app_(mk("ev-decision", `<h4>最终决策</h4><pre>${esc(txt)}</pre>`)); return;
  }

  if (ev === "validation_result") {
    const ok = d.approved;
    app_(mk("ev-sys", `验证：${ok?"✓ 通过":"✗ 未通过"}  ${d.summary||""}`)); return;
  }

  if (ev === "session_end") {
    _typeCompleteAll();
    const ok = d.outcome === "success";
    badge.textContent = ok ? "完成" : d.outcome;
    badge.className   = ok ? "done" : "error";
    app_(mk("ev-done" + (ok ? "" : " error"),
      `会话结束 · ${d.outcome}  共 ${d.total_rounds} 轮  耗时 ${(d.duration_s||0).toFixed(1)}s`));
    return;
  }
}

sse.onmessage = e => { try { render(JSON.parse(e.data)); } catch {} };
sse.onerror   = () => { badge.textContent = "连接断开"; badge.className = ""; };
</script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return _HTML, 200, {"Content-Type": "text/html; charset=utf-8"}


@app.route("/events")
def events():
    live    = request.args.get("live") == "1"
    log_arg = request.args.get("log", "").strip()

    def stream_live():
        """实时模式：本连接独享队列，接收 push_event 广播的全部事件。"""
        q: _queue.SimpleQueue = _queue.SimpleQueue()
        # 在同一把 backlog 锁内快照已发生事件并注册订阅，确保 push_event 串行化，
        # 新连接既不漏事件也不重复（详见 push_event 的加锁顺序）。
        with _backlog_lock:
            backlog_snapshot = list(_backlog)
            with _subscribers_lock:
                _subscribers.add(q)
        _client_ready.set()   # 通知 orchestrator 可以开始运行
        try:
            # 先补播本会话已发生的事件（让刷新/重连的页面从头重建），再接 live 流
            for d in backlog_snapshot:
                yield f"data: {json.dumps(d, ensure_ascii=False)}\n\n"
            while True:
                try:
                    d = q.get(timeout=15)
                    yield f"data: {json.dumps(d, ensure_ascii=False)}\n\n"
                    if d.get("event") == "session_end":
                        return
                except _queue.Empty:
                    yield ": ka\n\n"  # keepalive
        finally:
            with _subscribers_lock:
                _subscribers.discard(q)

    def stream_replay():
        """回放模式：tail JSONL 文件（用于查看历史会话）。"""
        log_path: Path | None = Path(log_arg) if log_arg else None

        if not log_path:
            yield "data: " + json.dumps({"event": "_wait", "msg": "等待日志文件..."}) + "\n\n"
            log_dir = Path("logs")
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                files = sorted(log_dir.glob("session_*.jsonl"),
                               key=lambda p: p.stat().st_mtime, reverse=True)
                if files:
                    log_path = files[0]
                    break
                time.sleep(0.5)
                yield ": ka\n\n"

        if not log_path or not log_path.exists():
            yield "data: " + json.dumps({"event": "_error", "msg": "未找到日志文件"}) + "\n\n"
            return

        with open(log_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    yield f"data: {line}\n\n"
            while True:
                line = f.readline()
                if line:
                    line = line.strip()
                    if line:
                        yield f"data: {line}\n\n"
                else:
                    time.sleep(0.2)
                    yield ": ka\n\n"

    gen = stream_live() if live else stream_replay()

    def _as_bytes(chunks):
        # direct_passthrough=True 下 werkzeug 要求写入 bytes；生成器产出 str 需在此编码。
        for chunk in chunks:
            yield chunk.encode("utf-8") if isinstance(chunk, str) else chunk

    return Response(
        stream_with_context(_as_bytes(gen)),
        mimetype="text/event-stream",
        direct_passthrough=True,
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


_MEMORY_HTML = r"""<!doctype html>
<html lang="zh"><head><meta charset="utf-8">
<title>Multi-AP 记忆健康度</title>
<style>
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: 'SF Mono','Consolas','Menlo',monospace; background: #111827; color: #d1d5db; padding: 18px; }
h1 { font-size: 17px; color: #93c5fd; margin-bottom: 4px; }
.sub { font-size: 12px; color: #6b7280; margin-bottom: 16px; }
.grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)); gap: 14px; }
.card { background: #1f2937; border: 1px solid #374151; border-radius: 6px; padding: 13px 15px; }
.card h2 { font-size: 13px; color: #9ca3af; margin-bottom: 10px; letter-spacing: .5px; }
.row { display: flex; justify-content: space-between; font-size: 13px; padding: 3px 0; }
.row .k { color: #9ca3af; }
.row .v { color: #f9fafb; font-weight: bold; }
.v.warn { color: #fbbf24; } .v.bad { color: #f87171; } .v.good { color: #4ade80; }
.bars { margin-top: 6px; }
.bar { display: flex; align-items: center; gap: 6px; font-size: 12px; padding: 2px 0; }
.bar .lbl { width: 82px; color: #9ca3af; } .bar .fill { height: 9px; border-radius: 2px; }
</style></head><body>
<h1>Multi-AP 记忆健康度</h1>
<div class="sub" id="ts">加载中…</div>
<div class="grid" id="grid"></div>
<script>
const VCOLOR = { improved:"#4ade80", neutral:"#9ca3af", degraded:"#f87171", inconclusive:"#60a5fa", unevaluated:"#4b5563" };
function row(k, v, cls){ return `<div class="row"><span class="k">${k}</span><span class="v ${cls||''}">${v}</span></div>`; }
function bars(obj, total){
  total = total || Object.values(obj).reduce((a,b)=>a+b,0) || 1;
  return '<div class="bars">' + Object.entries(obj).map(([k,v])=>{
    const w = Math.max(3, Math.round(v/total*140));
    return `<div class="bar"><span class="lbl">${k}</span><span class="fill" style="width:${w}px;background:${VCOLOR[k]||'#6b7280'}"></span><span>${v}</span></div>`;
  }).join('') + '</div>';
}
function card(title, html){ return `<div class="card"><h2>${title}</h2>${html}</div>`; }
function render(d){
  document.getElementById('ts').textContent = '生成于 ' + d.generated_at;
  const g = document.getElementById('grid');
  const ep = d.episodes, q = ep.quality || {};
  const degCls = ep.degraded_ratio > 0.3 ? 'bad' : (ep.degraded_ratio > 0.1 ? 'warn' : 'good');
  const ev = d.evaluations, ru = d.rules, rn = d.runs;
  g.innerHTML =
    card('Runs', row('总数', rn.total) + row('已完成', rn.completed, 'good') + row('未完成', rn.incomplete, rn.incomplete?'warn':'')) +
    card('案例 Episodes', row('存活', ep.alive, 'good') + row('已归档', ep.archived) +
      row('质量中位数', (q.median!==undefined?q.median:'—')) + row('恶化占比', (ep.degraded_ratio*100).toFixed(0)+'%', degCls) + bars(ep.by_verdict)) +
    card('评估窗口 Evaluations', row('待结算 pending', ev.pending, ev.pending?'warn':'') +
      row('已结算 collected', ev.collected, 'good') + row('已放弃 abandoned', ev.abandoned, ev.abandoned?'bad':'')) +
    card('规律 Rules', row('生效 active', ru.active, 'good') + row('冲突 conflicted', ru.conflicted, ru.conflicted?'warn':'') +
      row('平均置信度', (ru.avg_confidence!==null?ru.avg_confidence:'—')) + bars(ru.by_verdict)) +
    card('拓扑 Topologies', row('拓扑数', d.topologies.count) + row('单拓扑最多存活', d.topologies.max_alive_per_topology));
}
function refresh(){ fetch('/memory/health').then(r=>r.json()).then(render).catch(()=>{}); }
refresh(); setInterval(refresh, 15000);
</script></body></html>
"""


@app.route("/memory")
def memory_page():
    return _MEMORY_HTML, 200, {"Content-Type": "text/html; charset=utf-8"}


@app.route("/memory/health")
def memory_health_json():
    from src.persistence import EventStore
    from src.memory import memory_health
    from src.logger import DEFAULT_EVENT_DB
    db = os.environ.get("MULTIAP_EVENT_DB", str(DEFAULT_EVENT_DB))
    store = EventStore(db)
    try:
        return jsonify(memory_health(store))
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": str(exc)}), 500
    finally:
        store.close()


@app.route("/metrics")
def metrics():
    try:
        r = _req.get(f"{_STATE_SERVER}/history", timeout=3)
        return r.content, r.status_code, {"Content-Type": "application/json"}
    except Exception:
        return jsonify({}), 503


@app.route("/push", methods=["POST"])
def push():
    """接收外部进程（run_openclaw）推来的事件 JSON，转发给实时订阅者。

    让常驻 dashboard（由 serve.sh 起的独立进程）也能收到 run_openclaw 的
    SessionLogger 事件流——run_openclaw 不再在本进程内起 dashboard 线程，
    而是把 event_sink 指向此端点，事件经 HTTP 进来再由 push_event 广播。
    """
    d = request.get_json(silent=True)
    if not isinstance(d, dict):
        return jsonify({"ok": False, "error": "body must be a JSON object"}), 400
    push_event(d)
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    global _STATE_SERVER
    p = argparse.ArgumentParser(description="协商 Dashboard 服务")
    p.add_argument("--port", type=int, default=5050)
    p.add_argument("--state-server", default=_STATE_SERVER,
                   help="state server 地址（默认 http://localhost:5001）")
    args = p.parse_args()
    _STATE_SERVER = args.state_server
    print(f"Dashboard 启动于 http://0.0.0.0:{args.port}")
    app.run(host="0.0.0.0", port=args.port, debug=False, threaded=True)


if __name__ == "__main__":
    main()
