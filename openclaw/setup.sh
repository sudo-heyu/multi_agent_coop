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

# 2) 写入 profile 配置（ollama + 可选 PPIO provider + agent + 默认模型）
mkdir -p "$CFG_DIR"
TOKEN="$(openssl rand -hex 24)"
# PPIO key：优先环境变量，否则从仓库 .env 读取（不写入仓库内任何文件）
PPIO_KEY="${PPIO_API_KEY:-$(sed -n 's/^PPIO_API_KEY=//p' "$REPO/.env" 2>/dev/null)}"
PPIO_MODEL_ID="qwen/qwen3-next-80b-a3b-instruct"
# 默认模型：有 PPIO key 用云端 80b（更稳更快），否则本地 ollama
if [ -n "$PPIO_KEY" ]; then
  MODEL_REF="${MULTIAP_MODEL_REF:-ppio/$PPIO_MODEL_ID}"
else
  MODEL_REF="${MULTIAP_MODEL_REF:-ollama/$OLLAMA_MODEL}"
fi
echo "[setup] default model = $MODEL_REF  (ppio_key=$([ -n "$PPIO_KEY" ] && echo yes || echo no))"
"$PY" - "$CFG" "$REPO" "$TOKEN" "$OLLAMA_MODEL" "$PPIO_KEY" "$PPIO_MODEL_ID" "$MODEL_REF" "${AGENTS[@]}" <<'PYEOF'
import json, sys
cfg, repo, token, ollama_model, ppio_key, ppio_model_id, model_ref, *agents = sys.argv[1:]
providers = {"ollama": {
    "baseUrl": "http://localhost:11434", "apiKey": "ollama-local", "api": "ollama",
    "models": [{"id": ollama_model, "name": ollama_model, "input": ["text"]}]}}
if ppio_key:
    providers["ppio"] = {
        "baseUrl": "https://api.ppio.com/openai/v1", "apiKey": ppio_key,
        "api": "openai-completions",
        "models": [{"id": ppio_model_id, "name": "qwen:80b", "input": ["text"]}]}
conf = {
    "meta": {"lastTouchedVersion": "multiap-setup"},
    "gateway": {"mode": "local", "bind": "loopback",
                "auth": {"mode": "token", "token": token}},
    "models": {"providers": providers},
    "agents": {
        "defaults": {"workspace": f"{repo}/openclaw/workspaces/ap1",
                     "skipBootstrap": True, "model": {"primary": model_ref}},
        "list": [{"id": a, "default": (a == "ap1"),
                  "workspace": f"{repo}/openclaw/workspaces/{a}"} for a in agents],
    },
}
open(cfg, "w", encoding="utf-8").write(json.dumps(conf, ensure_ascii=False, indent=2))
print(f"[setup] wrote {cfg}")
PYEOF

# 3) 注册 MCP 工具服务
"$OPENCLAW" --profile "$PROFILE" mcp set multiap-tools \
  "{\"command\":\"$PY\",\"args\":[\"$REPO/openclaw/mcp/multiap_mcp.py\"],\"env\":{\"MULTIAP_STATE_SERVER\":\"$STATE_SERVER\",\"MULTIAP_PROFILE\":\"$PROFILE\"}}" >/dev/null

# 4) 校验
"$OPENCLAW" --profile "$PROFILE" config validate
echo "[setup] done. 冒烟测试："
echo "  OLLAMA_API_KEY=ollama-local $OPENCLAW --profile $PROFILE agent --local --agent ap1 --thinking off -m 'hi' --json"
