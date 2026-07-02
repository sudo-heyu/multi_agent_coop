#!/usr/bin/env bash
# 在隔离的 openclaw profile `multiap` 下配置 Multi-AP 纯 OpenClaw 架构。
# 不触碰用户已有的 ~/.openclaw 默认 profile（C3-PO）。
#
# 用法：  bash openclaw/setup.sh
# 依赖：  已安装 openclaw（Node）+ ollama 运行中 + 一个带 mcp 包的 python。
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
PROFILE="${MULTIAP_PROFILE:-multiap}"
OPENCLAW="${OPENCLAW_BIN:-$(command -v openclaw || true)}"
OPENCLAW="${OPENCLAW:-$HOME/.openclaw/bin/openclaw}"
PY="${MULTIAP_PY:-$(command -v python3)}"
STATE_SERVER="${MULTIAP_STATE_SERVER:-http://localhost:5001}"
OLLAMA_MODEL="${MULTIAP_MODEL:-qwen3:14b}"
CFG_DIR="$HOME/.openclaw-$PROFILE"
CFG="$CFG_DIR/openclaw.json"
# qwen3-next-80b-a3b-instruct 2026-07 起在 PPIO 下架（列表仍在但推理返回
# MODEL_NOT_AVAILABLE），默认改用其后继 MoE instruct 模型。
PPIO_MODEL_ID="${MULTIAP_PPIO_MODEL_ID:-qwen/qwen3.6-35b-a3b}"
PPIO_MODEL_ALIAS="${MULTIAP_PPIO_MODEL_ALIAS:-qwen80binstruct}"
PPIO_MODEL_NAME="${MULTIAP_PPIO_MODEL_NAME:-qwen80binstruct}"
# 常驻 gateway 端口（serve.sh / drive_ap 都从此处读）。OpenClaw 默认给本 profile 的
# launchd gateway 服务也用 18789，二者对齐即可让 AP 回合走常驻 gateway。
GATEWAY_PORT="${MULTIAP_GATEWAY_PORT:-18789}"

AGENTS=(coordinator ap1 ap2 ap3)   # coordinator 只做阶段级触发，AP 负责自主协商内容

echo "[setup] repo=$REPO profile=$PROFILE python=$PY model=ollama/$OLLAMA_MODEL"

if ! "$PY" -c 'import mcp.server.fastmcp' >/dev/null 2>&1; then
  echo "[setup] ERROR: Python 缺少 mcp 包。请先运行：$PY -m pip install -r $REPO/requirements.txt" >&2
  exit 1
fi

# 1) 确保每个 agent 的 workspace 目录存在（内容由各 workspace 的 *.md 提供）
for a in "${AGENTS[@]}"; do
  mkdir -p "$REPO/openclaw/workspaces/$a"
  mkdir -p "$CFG_DIR/agents/$a/sessions"
  if [ ! -e "$REPO/openclaw/workspaces/$a/IDENTITY.md" ]; then
    printf '# 身份\n\n你是 %s。\n' "$a" > "$REPO/openclaw/workspaces/$a/IDENTITY.md"
  fi
done

# 2) 写入 profile 配置（ollama + 可选 PPIO provider + agent + 默认模型）
mkdir -p "$CFG_DIR"
TOKEN="$(openssl rand -hex 24)"
# PPIO key：优先环境变量，否则从仓库 .env 读取（不写入仓库内任何文件）
PPIO_KEY="${PPIO_API_KEY:-$(sed -n 's/^PPIO_API_KEY=//p' "$REPO/.env" 2>/dev/null)}"
# 默认模型：有 PPIO key 用云端 80b（更稳更快），否则本地 ollama
if [ -n "$PPIO_KEY" ]; then
  MODEL_REF="${MULTIAP_MODEL_REF:-$PPIO_MODEL_ALIAS}"
else
  MODEL_REF="${MULTIAP_MODEL_REF:-ollama/$OLLAMA_MODEL}"
fi
echo "[setup] default model = $MODEL_REF  (ppio_key=$([ -n "$PPIO_KEY" ] && echo yes || echo no))"
"$PY" - "$CFG" "$REPO" "$TOKEN" "$OLLAMA_MODEL" "$PPIO_KEY" "$PPIO_MODEL_ID" "$PPIO_MODEL_ALIAS" "$PPIO_MODEL_NAME" "$MODEL_REF" "$GATEWAY_PORT" "${AGENTS[@]}" <<'PYEOF'
import json, sys
cfg, repo, token, ollama_model, ppio_key, ppio_model_id, ppio_model_alias, ppio_model_name, model_ref, gateway_port, *agents = sys.argv[1:]
ppio_ref = f"ppio/{ppio_model_id}"
providers = {"ollama": {
    "baseUrl": "http://localhost:11434", "apiKey": "ollama-local", "api": "ollama",
    "models": [{"id": ollama_model, "name": ollama_model, "input": ["text"]}]}}
default_models = {
    f"ollama/{ollama_model}": {"alias": "local-qwen"}
}
if ppio_key:
    providers["ppio"] = {
        "baseUrl": "https://api.ppio.com/openai/v1", "apiKey": ppio_key,
        "api": "openai-completions",
        "models": [{
            "id": ppio_model_id,
            "name": ppio_model_name,
            "input": ["text"],
            "compat": {"thinkingFormat": "qwen", "supportsStrictMode": False},
        }]}
    default_models[ppio_ref] = {
        "alias": ppio_model_alias,
        "params": {"temperature": 0.2},
    }
# 仅 coordinator 可调用的编排/验收/下发工具；AP agent 一律禁用，避免子 agent
# 误触发整轮协商或越权下发（MCP 工具运行时名为 <server>__<tool>）。
coordinator_only = [
    "multiap-tools__run_fast_negotiation",
]

def _agent_entry(a):
    entry = {"id": a, "default": (a == "coordinator"),
             "workspace": f"{repo}/openclaw/workspaces/{a}"}
    if a != "coordinator":
        entry["tools"] = {"deny": coordinator_only}
    return entry

conf = {
    "meta": {"lastTouchedVersion": "multiap-setup"},
    "gateway": {"mode": "local", "bind": "loopback", "port": int(gateway_port),
                "auth": {"mode": "token", "token": token}},
    "models": {"providers": providers},
    "agents": {
        "defaults": {"workspace": f"{repo}/openclaw/workspaces/coordinator",
                     "skipBootstrap": True,
                     "models": default_models,
                     "model": {"primary": model_ref}},
        "list": [_agent_entry(a) for a in agents],
    },
}
open(cfg, "w", encoding="utf-8").write(json.dumps(conf, ensure_ascii=False, indent=2))
print(f"[setup] wrote {cfg}")
PYEOF

# 3) 注册 MCP 工具服务
# MULTIAP_SESSION_LOG=1 写进注册 env：MCP server 只继承注册 env（不继承调用方进程 env），
# 故在此开启会话 JSONL，coordinator 路径才会落日志，run_openclaw 方能 tail 出实时对话。
"$OPENCLAW" --profile "$PROFILE" mcp set multiap-tools \
  "{\"command\":\"$PY\",\"args\":[\"$REPO/openclaw/mcp/multiap_mcp.py\"],\"requestTimeoutMs\":600000,\"connectionTimeoutMs\":30000,\"env\":{\"MULTIAP_STATE_SERVER\":\"$STATE_SERVER\",\"MULTIAP_PROFILE\":\"$PROFILE\",\"OPENCLAW_BIN\":\"$OPENCLAW\",\"MULTIAP_SESSION_LOG\":\"1\",\"NO_PROXY\":\"localhost,127.0.0.1,::1\",\"no_proxy\":\"localhost,127.0.0.1,::1\"}}" >/dev/null

# 4) 校验
"$OPENCLAW" --profile "$PROFILE" config validate
echo "[setup] done. 冒烟测试："
echo "  OLLAMA_API_KEY=ollama-local $OPENCLAW --profile $PROFILE agent --local --agent ap1 --thinking off -m 'hi' --json"
