#!/usr/bin/env bash
# 绑定启动 Multi-AP 的后台长期服务：state server(--allow-mock) + 常驻 multiap gateway。
# 常驻 gateway 让协商时的 AP 回合免去每回合的 runtime/MCP 冷启动（见 orchestration.drive_ap）。
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
STATE_URL="http://localhost:${STATE_PORT}"
RUN_DIR="$HOME/.openclaw-$PROFILE/run"
STATE_PID="$RUN_DIR/state.pid"
GW_PID="$RUN_DIR/gateway.pid"          # 仅当本脚本 nohup 兜底起 gateway 时写
STATE_LOG="$RUN_DIR/state.log"
GW_LOG="$RUN_DIR/gateway.log"
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

mkdir -p "$RUN_DIR"

port_listening() { lsof -nP -iTCP:"$1" -sTCP:LISTEN >/dev/null 2>&1; }
# --noproxy '*'：本机若设了 http_proxy，localhost 探活会被代理拦截，必须绕过
state_alive()    { curl -sf -m 2 --noproxy '*' "$STATE_URL/health" >/dev/null 2>&1; }
launchd_exists() { launchctl print "gui/$(id -u)/$LAUNCHD_LABEL" >/dev/null 2>&1; }

start_state() {
    if state_alive; then echo "[serve] state server 已在线: $STATE_URL"; return 0; fi
    if port_listening "$STATE_PORT"; then
        echo "[serve] 端口 $STATE_PORT 被占用但 /health 不通，跳过 state server"; return 0
    fi
    echo "[serve] 启动 state server (--allow-mock) ..."
    NO_PROXY=localhost,127.0.0.1,::1 no_proxy=localhost,127.0.0.1,::1 \
        nohup "$PY" "$REPO/state_server/server.py" --allow-mock >"$STATE_LOG" 2>&1 &
    echo $! > "$STATE_PID"
    for _ in $(seq 1 15); do if state_alive; then break; fi; sleep 1; done
    if state_alive; then
        echo "[serve] state server UP: $STATE_URL (pid $(cat "$STATE_PID"))"
    else
        echo "[serve] state server 启动失败，见 $STATE_LOG"; return 1
    fi
}

start_gateway() {
    if port_listening "$GW_PORT"; then echo "[serve] gateway 已在线: :${GW_PORT} (launchd 长期服务)"; return 0; fi
    if launchd_exists; then
        echo "[serve] kickstart launchd 服务 $LAUNCHD_LABEL ..."
        launchctl kickstart -k "gui/$(id -u)/$LAUNCHD_LABEL" 2>/dev/null || true
    else
        echo "[serve] 未发现 launchd 服务，nohup 兜底起 gateway :$GW_PORT ..."
        OLLAMA_API_KEY=ollama-local NO_PROXY=localhost,127.0.0.1,::1 no_proxy=localhost,127.0.0.1,::1 \
            nohup "$OPENCLAW" --profile "$PROFILE" gateway --port "$GW_PORT" --bind loopback >"$GW_LOG" 2>&1 &
        echo $! > "$GW_PID"
    fi
    for _ in $(seq 1 45); do if port_listening "$GW_PORT"; then break; fi; sleep 1; done
    if port_listening "$GW_PORT"; then
        echo "[serve] gateway UP: ws://127.0.0.1:$GW_PORT"
    else
        echo "[serve] gateway 启动失败，见 $GW_LOG / Console.app"; return 1
    fi
}

stop_state() {
    if [ -f "$STATE_PID" ]; then
        local pid; pid="$(cat "$STATE_PID" 2>/dev/null || true)"
        if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
            kill "$pid" 2>/dev/null || true; echo "[serve] 已停止 state server (pid $pid)"
        else echo "[serve] state server 进程已不在"; fi
        rm -f "$STATE_PID"
    else echo "[serve] state server 无 PID 文件（非 serve.sh 启动，不强杀 :$STATE_PORT）"; fi
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

restart_gateway() {
    # 改了 setup.sh / mcp 注册 / 配置后用它重载 gateway（否则常驻 gateway 缓存旧 MCP 连接，
    # 会出现 AP 调工具时 "tool isn't available"）。
    if launchd_exists; then
        echo "[serve] 重启 launchd gateway $LAUNCHD_LABEL（重载 MCP/配置）..."
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
}

case "${1:-}" in
    start)   start_state; start_gateway ;;
    stop)    stop_gateway; stop_state ;;
    status)  status ;;
    restart) stop_state; sleep 1; start_state; restart_gateway ;;
    *) echo "用法: bash openclaw/serve.sh {start|stop|status|restart}"; exit 1 ;;
esac
