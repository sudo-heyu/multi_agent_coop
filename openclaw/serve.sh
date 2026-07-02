#!/usr/bin/env bash
# 绑定启动 Multi-AP 的后台长期服务：state server + 常驻 multiap gateway
# + dashboard + academic plot。一条命令全开，run_openclaw 强制复用它们（不在线则提示
# 先 `bash openclaw/serve.sh start`），协商时零临时服务启动。常驻 gateway 让 AP 回合
# 免去每回合 runtime/MCP 冷启动（见 orchestration.drive_ap）。
#
# 关于 gateway：OpenClaw 已为 multiap profile 注册了 launchd 服务 ai.openclaw.<profile>
# （RunAtLoad + KeepAlive，开机自启/崩溃自拉起，本身就是长期服务）。本脚本优先复用它，
# 不另起竞争 gateway；仅在其缺失时才用 nohup 兜底。stop 不会杀 launchd 托管的 gateway。
#
# 用法： bash openclaw/serve.sh {start|stop|status|restart}
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
PROFILE="${MULTIAP_PROFILE:-multiap}"
OPENCLAW="${OPENCLAW_BIN:-$(command -v openclaw || true)}"
OPENCLAW="${OPENCLAW:-$HOME/.openclaw/bin/openclaw}"
PY="${MULTIAP_PY:-$REPO/.venv/bin/python}"
STATE_PORT="${MULTIAP_STATE_PORT:-5001}"
STATE_MODE="${MULTIAP_STATE_MODE:-mock}"
STATE_URL="http://localhost:${STATE_PORT}"
RUN_DIR="$HOME/.openclaw-$PROFILE/run"
STATE_PID="$RUN_DIR/state.pid"
GW_PID="$RUN_DIR/gateway.pid"          # 仅当本脚本 nohup 兜底起 gateway 时写
STATE_LOG="$RUN_DIR/state.log"
GW_LOG="$RUN_DIR/gateway.log"
RAW_STREAM_ENABLED="${MULTIAP_OPENCLAW_RAW_STREAM:-1}"
RAW_STREAM_PATH="${MULTIAP_RAW_STREAM_PATH:-$RUN_DIR/../logs/raw-stream.jsonl}"
LAUNCHD_LABEL="ai.openclaw.${PROFILE}"

# gateway 端口：env 覆盖 > profile 配置 gateway.port > 18789（OpenClaw 默认，与 drive_ap 解析一致）
CFG_PORT="$("$PY" - "$PROFILE" <<'PYEOF'
import json, os, sys
profile = sys.argv[1]
home = os.environ.get("OPENCLAW_HOME") or os.path.expanduser("~")
try:
    d = json.load(open(os.path.join(home, f".openclaw-{profile}", "openclaw.json")))
    print((d.get("gateway") or {}).get("port") or "")
except Exception:
    print("")
PYEOF
)"
GW_PORT="${MULTIAP_GATEWAY_PORT:-${CFG_PORT:-18789}}"
DASH_PORT="${MULTIAP_DASHBOARD_PORT:-5050}"
DASH_PID="$RUN_DIR/dashboard.pid"
DASH_LOG="$RUN_DIR/dashboard.log"
# academic plot：matplotlib GUI 窗口（真实/mock 通用可视化）。无桌面时 start 跳过。
PLOT_PID="$RUN_DIR/plot.pid"
PLOT_LOG="$RUN_DIR/plot.log"

mkdir -p "$RUN_DIR"

port_listening() { lsof -nP -iTCP:"$1" -sTCP:LISTEN >/dev/null 2>&1; }
# --noproxy '*'：本机若设了 http_proxy，localhost 探活会被代理拦截，必须绕过
state_alive()    { curl -sf -m 2 --noproxy '*' "$STATE_URL/health" >/dev/null 2>&1; }
dash_alive()     { curl -sf -m 2 --noproxy '*' "http://localhost:${DASH_PORT}/" >/dev/null 2>&1; }
# plot 是 matplotlib GUI 窗口（无 HTTP 端口），用 pid 文件 + 进程存活判断。
# plt.show() 阻塞主线程，窗口关闭即进程退出，故 pid 存活 ≈ 窗口在。
plot_alive()     { [ -f "$PLOT_PID" ] || return 1
                   local pid; pid="$(cat "$PLOT_PID" 2>/dev/null || true)"
                   [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; }
launchd_exists() { launchctl print "gui/$(id -u)/$LAUNCHD_LABEL" >/dev/null 2>&1; }

ensure_launchd_raw_stream_env() {
    [ "$RAW_STREAM_ENABLED" != "0" ] || return 1
    local env_file="$HOME/.openclaw-$PROFILE/service-env/${LAUNCHD_LABEL}.env"
    [ -f "$env_file" ] || return 1
    local tmp_file="${env_file}.tmp.$$"
    awk '!/^export OPENCLAW_RAW_STREAM=/ && !/^export OPENCLAW_RAW_STREAM_PATH=/' "$env_file" > "$tmp_file"
    printf "export OPENCLAW_RAW_STREAM='%s'\n" "$RAW_STREAM_ENABLED" >> "$tmp_file"
    printf "export OPENCLAW_RAW_STREAM_PATH='%s'\n" "$RAW_STREAM_PATH" >> "$tmp_file"
    if cmp -s "$env_file" "$tmp_file"; then
        rm -f "$tmp_file"
        return 1
    fi
    mv "$tmp_file" "$env_file"
    echo "[serve] 已为 launchd gateway 启用 raw-stream: $RAW_STREAM_PATH"
    return 0
}

start_state() {
    if state_alive; then echo "[serve] state server 已在线: $STATE_URL"; return 0; fi
    if port_listening "$STATE_PORT"; then
        echo "[serve] 端口 $STATE_PORT 被占用但 /health 不通，跳过 state server"; return 0
    fi
    local state_args=()
    if [ "$STATE_MODE" = "mock" ]; then
        state_args=(--allow-mock)
    elif [ "$STATE_MODE" != "real" ]; then
        echo "[serve] MULTIAP_STATE_MODE 仅支持 mock/real，当前为: $STATE_MODE"; return 1
    fi
    echo "[serve] 启动 state server (mode=$STATE_MODE) ..."
    NO_PROXY=localhost,127.0.0.1,::1 no_proxy=localhost,127.0.0.1,::1 \
        nohup "$PY" "$REPO/state_server/server.py" "${state_args[@]}" >"$STATE_LOG" 2>&1 &
    echo $! > "$STATE_PID"
    for _ in $(seq 1 15); do if state_alive; then break; fi; sleep 1; done
    if state_alive; then
        echo "[serve] state server UP: $STATE_URL (pid $(cat "$STATE_PID"))"
    else
        echo "[serve] state server 启动失败，见 $STATE_LOG"; return 1
    fi
}

start_gateway() {
    if port_listening "$GW_PORT"; then
        echo "[serve] gateway 已在线: :${GW_PORT} (launchd 长期服务)"
        if ensure_launchd_raw_stream_env; then
            echo "[serve] 提示：raw-stream 环境已写入；请执行 bash openclaw/serve.sh restart 让当前 gateway 生效。"
        fi
        return 0
    fi
    if launchd_exists; then
        ensure_launchd_raw_stream_env || true
        echo "[serve] kickstart launchd 服务 $LAUNCHD_LABEL ..."
        launchctl kickstart -k "gui/$(id -u)/$LAUNCHD_LABEL" 2>/dev/null || true
    else
        echo "[serve] 未发现 launchd 服务，nohup 兜底起 gateway :$GW_PORT ..."
        local raw_args=()
        if [ "$RAW_STREAM_ENABLED" != "0" ]; then
            raw_args=(--raw-stream --raw-stream-path "$RAW_STREAM_PATH")
        fi
        OLLAMA_API_KEY=ollama-local OPENCLAW_RAW_STREAM="$RAW_STREAM_ENABLED" OPENCLAW_RAW_STREAM_PATH="$RAW_STREAM_PATH" \
            NO_PROXY=localhost,127.0.0.1,::1 no_proxy=localhost,127.0.0.1,::1 \
            nohup "$OPENCLAW" --profile "$PROFILE" gateway --port "$GW_PORT" --bind loopback "${raw_args[@]}" >"$GW_LOG" 2>&1 &
        echo $! > "$GW_PID"
    fi
    for _ in $(seq 1 45); do if port_listening "$GW_PORT"; then break; fi; sleep 1; done
    if port_listening "$GW_PORT"; then
        echo "[serve] gateway UP: ws://127.0.0.1:$GW_PORT"
    else
        echo "[serve] gateway 启动失败，见 $GW_LOG / Console.app"; return 1
    fi
}

start_dashboard_svc() {
    if dash_alive; then echo "[serve] dashboard 已在线: http://localhost:${DASH_PORT}/"; return 0; fi
    if port_listening "$DASH_PORT"; then
        echo "[serve] 端口 $DASH_PORT 被占用但不可达，跳过 dashboard"; return 0
    fi
    echo "[serve] 启动 Dashboard :$DASH_PORT ..."
    NO_PROXY=localhost,127.0.0.1,::1 no_proxy=localhost,127.0.0.1,::1 \
        nohup "$PY" "$REPO/dashboard/app.py" --port "$DASH_PORT" --state-server "$STATE_URL" >"$DASH_LOG" 2>&1 &
    echo $! > "$DASH_PID"
    for _ in $(seq 1 15); do if dash_alive; then break; fi; sleep 1; done
    if dash_alive; then
        echo "[serve] dashboard UP: http://localhost:${DASH_PORT}/ (pid $(cat "$DASH_PID"))"
    else
        echo "[serve] dashboard 启动失败，见 $DASH_LOG"; return 1
    fi
}

start_plot() {
    if plot_alive; then echo "[serve] academic plot 已在线: pid $(cat "$PLOT_PID")"; return 0; fi
    # matplotlib GUI 窗口需桌面会话；Linux 无 DISPLAY 直接跳过（不当作失败）。
    # macOS 用原生后端无需 DISPLAY；Windows 同理。
    if [ "$(uname)" = "Linux" ] && [ -z "${DISPLAY:-}" ]; then
        echo "[serve] 跳过 academic plot：无 DISPLAY（matplotlib GUI 需桌面会话）"; return 0
    fi
    echo "[serve] 启动 academic plot（matplotlib 窗口）..."
    NO_PROXY=localhost,127.0.0.1,::1 no_proxy=localhost,127.0.0.1,::1 \
        nohup "$PY" "$REPO/state_server/academic_plot.py" --server "$STATE_URL" >"$PLOT_LOG" 2>&1 &
    echo $! > "$PLOT_PID"
    sleep 1
    if plot_alive; then
        echo "[serve] academic plot UP: pid $(cat "$PLOT_PID") (matplotlib 窗口)"
    else
        echo "[serve] academic plot 启动失败（可能无 GUI 后端），见 $PLOT_LOG"; rm -f "$PLOT_PID"; return 1
    fi
}

stop_state() {
    if [ -f "$STATE_PID" ]; then
        local pid; pid="$(cat "$STATE_PID" 2>/dev/null || true)"
        if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
            kill "$pid" 2>/dev/null || true; echo "[serve] 已停止 state server (pid $pid)"
        else echo "[serve] state server 进程已不在"; fi
        rm -f "$STATE_PID"
    else echo "[serve] state server 无 PID 文件（非 serve.sh 启动，不强杀 :${STATE_PORT}）"; fi
}

stop_gateway() {
    # 只停本脚本 nohup 兜底起的 gateway；launchd 托管的（KeepAlive 长期服务）不动。
    if [ -f "$GW_PID" ]; then
        local pid; pid="$(cat "$GW_PID" 2>/dev/null || true)"
        if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
            kill "$pid" 2>/dev/null || true; echo "[serve] 已停止 nohup gateway (pid $pid)"
        fi
        rm -f "$GW_PID"
    elif launchd_exists; then
        echo "[serve] gateway 由 launchd 托管（KeepAlive 长期服务），未停止。"
        echo "        如确需停止： launchctl bootout gui/$(id -u)/$LAUNCHD_LABEL"
    else
        echo "[serve] 未发现 serve.sh 启动的 gateway"
    fi
}

stop_dashboard_svc() {
    if [ -f "$DASH_PID" ]; then
        local pid; pid="$(cat "$DASH_PID" 2>/dev/null || true)"
        if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
            kill "$pid" 2>/dev/null || true; echo "[serve] 已停止 dashboard (pid $pid)"
        else echo "[serve] dashboard 进程已不在"; fi
        rm -f "$DASH_PID"
    else echo "[serve] dashboard 无 PID 文件（非 serve.sh 启动，不强杀 :${DASH_PORT}）"; fi
}

stop_plot() {
    if [ -f "$PLOT_PID" ]; then
        local pid; pid="$(cat "$PLOT_PID" 2>/dev/null || true)"
        if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
            kill "$pid" 2>/dev/null || true; echo "[serve] 已停止 academic plot (pid $pid)"
        else echo "[serve] academic plot 进程已不在"; fi
        rm -f "$PLOT_PID"
    else echo "[serve] academic plot 无 PID 文件（非 serve.sh 启动）"; fi
}

restart_gateway() {
    # 改了 setup.sh / mcp 注册 / 配置后用它重载 gateway（否则常驻 gateway 缓存旧 MCP 连接，
    # 会出现 AP 调工具时 "tool isn't available"）。
    if launchd_exists; then
        ensure_launchd_raw_stream_env || true
        echo "[serve] 重启 launchd gateway ${LAUNCHD_LABEL}（重载 MCP/配置）..."
        launchctl kickstart -k "gui/$(id -u)/$LAUNCHD_LABEL" 2>/dev/null || true
        for _ in $(seq 1 45); do if port_listening "$GW_PORT"; then break; fi; sleep 1; done
        if port_listening "$GW_PORT"; then echo "[serve] gateway 已重启: :${GW_PORT}"; else echo "[serve] gateway 重启后未监听，见 Console.app"; fi
    else
        stop_gateway; sleep 1; start_gateway
    fi
}

status() {
    if state_alive; then echo "[serve] state server : UP   $STATE_URL"
    else echo "[serve] state server : down ($STATE_URL)"; fi
    if port_listening "$GW_PORT"; then
        local mgr="nohup"; launchd_exists && mgr="launchd:$LAUNCHD_LABEL"
        echo "[serve] gateway      : UP   ws://127.0.0.1:$GW_PORT ($mgr)"
    else
        echo "[serve] gateway      : down (:$GW_PORT)"
    fi
    if dash_alive; then echo "[serve] dashboard    : UP   http://localhost:${DASH_PORT}/"
    else echo "[serve] dashboard    : down (:$DASH_PORT)"; fi
    if plot_alive; then echo "[serve] academic plot: UP   pid $(cat "$PLOT_PID") (matplotlib 窗口)"
    else echo "[serve] academic plot: down (可选；无 GUI 时 serve.sh start 自动跳过)"; fi
}

case "${1:-}" in
    start)   start_state; start_gateway; start_dashboard_svc; start_plot || true ;;
    stop)    stop_plot; stop_dashboard_svc; stop_gateway; stop_state ;;
    status)  status ;;
    restart) stop_plot; stop_dashboard_svc; stop_state; sleep 1; start_state; restart_gateway; start_dashboard_svc; start_plot || true ;;
    *) echo "用法: bash openclaw/serve.sh {start|stop|status|restart}"; exit 1 ;;
esac
