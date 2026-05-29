"""
协商 Dashboard 服务（端口 5050）

左：SSE 实时对话流（tail JSONL 日志）
右：三项业务指标曲线图（轮询 state server /history）

由 run.py 自动启动，也可手动运行：
  python dashboard/app.py [--port 5050] [--state-server http://localhost:5001]
"""
import argparse
import json
import queue as _queue
import threading
import time
from pathlib import Path

from flask import Flask, Response, request, jsonify
import requests as _req

app = Flask(__name__)
_STATE_SERVER = "http://localhost:5001"

# ── 实时事件队列（主进程 orchestrator → dashboard SSE）──────────────────────
_live_queue:   _queue.SimpleQueue = _queue.SimpleQueue()
_client_ready: threading.Event   = threading.Event()   # 浏览器已连上 SSE 时置位


def push_event(d: dict) -> None:
    """由 orchestrator 线程调用，将事件推入实时队列。"""
    _live_queue.put(d)


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
.ev-strategy { padding: 8px 14px; border-radius: 4px; font-size: 13px; font-weight: bold; text-align: center; margin: 6px 0; }
.ev-strategy.co_sr   { background: #052e16; color: #4ade80; border: 1px solid #166534; }
.ev-strategy.co_edca { background: #0c1a2e; color: #60a5fa; border: 1px solid #1d4ed8; }
.ev-strategy.joint   { background: #1e1040; color: #c4b5fd; border: 1px solid #6d28d9; }

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
#charts-panel { display: flex; flex-direction: column; overflow: hidden; }
.chart-box { flex: 1; display: flex; flex-direction: column; border-bottom: 1px solid #1f2937; padding: 8px 16px 6px; min-height: 0; }
.chart-box:last-child { border-bottom: none; }
.chart-box h3 { font-size: 12px; color: #9ca3af; margin-bottom: 6px; flex-shrink: 0; }
.chart-box canvas { flex: 1; min-height: 0; }
#metrics-ts { font-size: 11px; color: #4b5563; padding: 4px 16px; flex-shrink: 0; }
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
    <div class="chart-box"><h3>吞吐量 (Mbps)</h3><canvas id="c-tput"></canvas></div>
    <div class="chart-box"><h3>延迟 (ms)</h3><canvas id="c-lat"></canvas></div>
    <div class="chart-box"><h3>丢包率 (%)</h3><canvas id="c-loss"></canvas></div>
    <div id="metrics-ts">指标：等待数据...</div>
  </div>
</main>
<script>
// ── Chart.js ────────────────────────────────────────────────────
const AP_COLORS = {
  ap1: { border: "#e74c3c", bg: "rgba(231,76,60,0.1)" },
  ap2: { border: "#3498db", bg: "rgba(52,152,219,0.1)" },
  ap3: { border: "#2ecc71", bg: "rgba(46,204,113,0.1)" },
};
const APS = ["ap1","ap2","ap3"];
let charts = {};

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

const script = document.createElement("script");
script.src = "https://cdn.jsdelivr.net/npm/chartjs-adapter-date-fns@3.0.0/dist/chartjs-adapter-date-fns.bundle.min.js";
script.onload = () => {
  charts = {
    throughput_mbps: makeChart("c-tput", "throughput_mbps", "Mbps"),
    latency_ms:      makeChart("c-lat",  "latency_ms",      "ms"),
    packet_loss_pct: makeChart("c-loss", "packet_loss_pct", "%"),
  };

  function refreshMetrics() {
    fetch("/metrics").then(r => r.ok ? r.json() : {}).then(hist => {
      const fields = ["throughput_mbps", "latency_ms", "packet_loss_pct"];
      fields.forEach(f => {
        const ch = charts[f]; if (!ch) return;
        APS.forEach((ap, i) => {
          ch.data.datasets[i].data = (hist[ap] || []).map(r => ({ x: r.t, y: r[f] }));
        });
        ch.update();
    });
    document.getElementById("metrics-ts").textContent =
      "指标更新: " + new Date().toLocaleTimeString();
    }).catch(() => {});
  }
  refreshMetrics();
  setInterval(refreshMetrics, 10000);
};
document.head.appendChild(script);

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
let _streamEl    = null;
let _streamBody  = null;
let _streamAgent = null;
let _curSeg      = null;   // 当前活跃 segment

// ── 打字机（segment 队列） ────────────────────────────────────────
// 每个气泡有自己的 segment = {el: .body, buf: 待输出文字}
// RAF 按队列顺序逐 segment 输出，互不干扰。
const _TYPE_CPS_LIVE   = 80;
const _TYPE_CPS_REPLAY = 200;
const _typeSegs  = [];
let   _typeRafId  = null;
let   _typeLastTs = 0;

const _typeCPS = () => _isLive ? _TYPE_CPS_LIVE : _TYPE_CPS_REPLAY;

function _typeTick(ts) {
  if (!_typeSegs.some(s => s.buf.length > 0)) { _typeRafId = null; return; }
  if (!_typeLastTs) _typeLastTs = ts;
  const elapsed = ts - _typeLastTs;
  _typeLastTs   = ts;
  let budget    = Math.max(1, Math.round(_typeCPS() * elapsed / 1000));
  for (const seg of _typeSegs) {
    if (budget <= 0) break;
    if (!seg.buf.length) continue;
    const take = Math.min(budget, seg.buf.length);
    seg.el.textContent += seg.buf.slice(0, take);
    seg.buf = seg.buf.slice(take);
    budget -= take;
    sd();
    if (!seg.buf.length) seg.el.classList.remove("typing");
  }
  _typeRafId = _typeSegs.some(s => s.buf.length > 0) ? requestAnimationFrame(_typeTick) : null;
}

function _typeStartRAF() {
  if (!_typeRafId) { _typeLastTs = 0; _typeRafId = requestAnimationFrame(_typeTick); }
}

function _typeFlushAll() {
  if (_typeRafId) { cancelAnimationFrame(_typeRafId); _typeRafId = null; }
  for (const s of _typeSegs) { s.el.textContent += s.buf; s.el.classList.remove("typing"); }
  _typeSegs.length = 0; _typeLastTs = 0;
}

function agentClass(ag) {
  return ["ap1","ap2","ap3"].includes(ag) ? ag : "coordinator";
}
function agentName(ag) {
  return ag === "coordinator" ? "协调者" : ag.toUpperCase();
}

function render(d) {
  const ev = d.event;

  if (ev === "_wait" || ev === "_error") {
    app_(mk("ev-sys", d.msg || ev)); return;
  }

  if (ev === "session_start") {
    body.innerHTML = "";
    _typeFlushAll(); _curSeg = null;
    _streamEl = null; _streamBody = null; _streamAgent = null;
    document.getElementById("session-meta").textContent =
      `session=${d.session_id}  model=${d.model}  scene=${d.scene}`;
    badge.textContent = "运行中"; badge.className = "running";
    app_(mk("ev-sys", `会话开始 · 场景 ${(d.scene||"").toUpperCase()} · ${d.model}`));
    return;
  }

  if (ev === "strategy_decided") {
    const labels = { co_sr:"Co-SR  空间复用", co_edca:"Co-EDCA  拥塞控制", joint:"Joint  联合优化" };
    app_(mk("ev-strategy " + d.strategy, "策略：" + (labels[d.strategy] || d.strategy)));
    return;
  }

  if (ev === "phase_start" || ev === "tool_call") {
    return;
  }

  // 发言开始：为这个气泡创建新的 segment，加入打字机队列
  if (ev === "agent_speak_start") {
    const ag  = (d.agent||"").toLowerCase();
    const cls = agentClass(ag);
    if (_streamAgent === ag && _streamEl && _curSeg) {
      // 同一 agent 重试：清空当前 segment，重置气泡内容
      _curSeg.buf = ''; _streamBody.textContent = '';
      return;
    }
    const bubble = mk("ev-speak " + cls,
      `<div class="tag">${agentName(ag)}</div><div class="body"></div>`);
    _streamEl    = bubble;
    _streamBody  = bubble.querySelector(".body");
    _streamAgent = ag;
    _curSeg      = {el: _streamBody, buf: ''};
    _typeSegs.push(_curSeg);
    app_(bubble);
    return;
  }

  // 文本片段：追加到当前 segment 缓冲，触发打字机
  if (ev === "agent_speak_chunk") {
    if (_curSeg) {
      _curSeg.buf += (d.text || "");
      _curSeg.el.classList.add("typing");
      _typeStartRAF();
    }
    return;
  }

  // 发言完成：仅清除"谁在说话"状态，segment 继续在队列中被打字机消化
  if (ev === "agent_speak") {
    const ag = (d.agent||"").toLowerCase();
    if (_streamAgent === ag && _streamEl) {
      _streamEl = null; _streamBody = null; _streamAgent = null; _curSeg = null;
      return;
    }
    // 无前置 chunk 的回放：整段文字直接入队
    const cls = agentClass(ag);
    const bubble = mk("ev-speak " + cls,
      `<div class="tag">${agentName(ag)}</div><div class="body"></div>`);
    const bodyEl = bubble.querySelector(".body");
    _curSeg = {el: bodyEl, buf: d.response || ''};
    _typeSegs.push(_curSeg);
    bodyEl.classList.add("typing");
    app_(bubble);
    if (_curSeg.buf) _typeStartRAF();
    _streamEl = null; _streamBody = null; _streamAgent = null; _curSeg = null;
    return;
  }

  if (ev === "vote") {
    const rid = "vr-" + (d.round||0);
    let row = document.getElementById(rid);
    if (!row) { row = mk("votes-row"); row.id = rid; app_(row); }
    row.appendChild(mk("ev-vote " + (d.agreed ? "ok" : "no"),
      (d.agreed ? "✓" : "✗") + " " + (d.voter||"").toUpperCase()));
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
    _typeFlushAll(); _curSeg = null;
    _streamEl = null; _streamBody = null; _streamAgent = null;
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
        """实时模式：从内存队列读取，零延迟、零缓冲。"""
        _client_ready.set()   # 通知 orchestrator 可以开始运行
        while True:
            try:
                d = _live_queue.get(timeout=15)
                yield f"data: {json.dumps(d, ensure_ascii=False)}\n\n"
                if d.get("event") == "session_end":
                    return
            except _queue.Empty:
                yield ": ka\n\n"  # keepalive

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
    return Response(gen, content_type="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.route("/metrics")
def metrics():
    try:
        r = _req.get(f"{_STATE_SERVER}/history", timeout=3)
        return r.content, r.status_code, {"Content-Type": "application/json"}
    except Exception:
        return jsonify({}), 503


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
