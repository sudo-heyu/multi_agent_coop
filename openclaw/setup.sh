#!/usr/bin/env bash
# 在隔离的 openclaw profile `multiap` 下配置 Multi-AP 纯 OpenClaw 架构。
# 不触碰用户已有的 ~/.openclaw 默认 profile（C3-PO）。
#
# 用法：  bash openclaw/setup.sh
# 依赖：  已安装 openclaw（Node）+ ollama 运行中 + 一个带 mcp 包的 python。
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
PROFILE="${MULTIAP_PROFILE:-multiap}"
OPENCLAW="${OPENCLAW_BIN:-$HOME/.openclaw/bin/openclaw}"
PY="${MULTIAP_PY:-$(command -v python3)}"
STATE_SERVER="${MULTIAP_STATE_SERVER:-http://localhost:5001}"
OLLAMA_MODEL="${MULTIAP_MODEL:-qwen3:14b}"
CFG_DIR="$HOME/.openclaw-$PROFILE"
CFG="$CFG_DIR/openclaw.json"

AGENTS=(ap1 ap2 ap3)   # 架构 C：无协调者，三台 AP 自驱动协商

echo "[setup] repo=$REPO profile=$PROFILE python=$PY model=ollama/$OLLAMA_MODEL"

# 1) 确保每个 agent 的 workspace 目录存在（内容由各 workspace 的 *.md 提供）
for a in "${AGENTS[@]}"; do
  mkdir -p "$REPO/openclaw/workspaces/$a"
  if [ ! -e "$REPO/openclaw/workspaces/$a/IDENTITY.md" ]; then
    printf '# 身份\n\n我是 %s。\n' "$a" > "$REPO/openclaw/workspaces/$a/IDENTITY.md"
  fi
done

# 2) 写入 profile 配置（ollama provider + 4 个 agent + 默认模型）
mkdir -p "$CFG_DIR"
TOKEN="$(openssl rand -hex 24)"
"$PY" - "$CFG" "$REPO" "$TOKEN" "$STATE_SERVER" "$OLLAMA_MODEL" "${AGENTS[@]}" <<'PYEOF'
import json, sys
cfg, repo, token, state_server, model, *agents = sys.argv[1:]
conf = {
    "meta": {"lastTouchedVersion": "multiap-setup"},
    "gateway": {"mode": "local", "bind": "loopback",
                "auth": {"mode": "token", "token": token}},
    "models": {"providers": {"ollama": {
        "baseUrl": "http://localhost:11434", "apiKey": "ollama-local", "api": "ollama",
        "models": [{"id": model, "name": model, "input": ["text"]}]}}},
    "agents": {
        "defaults": {"workspace": f"{repo}/openclaw/workspaces/ap1",
                     "skipBootstrap": True, "model": {"primary": f"ollama/{model}"}},
        "list": [
            {"id": a, "default": (a == "ap1"),
             "workspace": f"{repo}/openclaw/workspaces/{a}"}
            for a in agents
        ],
    },
}
with open(cfg, "w", encoding="utf-8") as fh:
    json.dump(conf, fh, ensure_ascii=False, indent=2)
print(f"[setup] wrote {cfg}")
PYEOF

# 3) 注册 MCP 工具服务
"$OPENCLAW" --profile "$PROFILE" mcp set multiap-tools \
  "{\"command\":\"$PY\",\"args\":[\"$REPO/openclaw/mcp/multiap_mcp.py\"],\"env\":{\"MULTIAP_STATE_SERVER\":\"$STATE_SERVER\",\"MULTIAP_PROFILE\":\"$PROFILE\"}}" >/dev/null

# 4) 校验
"$OPENCLAW" --profile "$PROFILE" config validate
echo "[setup] done. 冒烟测试："
echo "  OLLAMA_API_KEY=ollama-local $OPENCLAW --profile $PROFILE agent --local --agent ap1 --thinking off -m 'hi' --json"
