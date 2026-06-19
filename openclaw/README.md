# 纯 OpenClaw 架构（迁移中）

把 Multi-AP 协商系统的**托管层**与**编排入口**交给 OpenClaw：
- 托管：`coordinator / ap1 / ap2 / ap3` 作为隔离的 OpenClaw agent，默认跑 PPIO `qwen80binstruct`，无 PPIO key 时才回退本机 ollama。
- 编排入口：`coordinator`（LLM）调用阶段级快速工具 `run_fast_negotiation`，一次性驱动广播→提案→投票→验收。
  coordinator 不逐句选择发言人，避免每轮都增加一次模型思考延迟。
- 阶段执行：MCP 工具内部批量唤醒 AP agents；AP 的发言内容、策略选择、提案和投票仍由 AP agents 自主完成。
- 确定性逻辑（Co-SR/Co-EDCA 计算、Validator、状态读取、下发）保留为 Python，
  经 **MCP 工具服务**（`openclaw/mcp/multiap_mcp.py`）暴露给 agent 调用，结果与现有实现一致。

所有配置在隔离 profile `multiap`（`~/.openclaw-multiap/`），不影响用户默认 profile。

## 目录
```
openclaw/
  setup.sh                 # 在 multiap profile 下配置 PPIO qwen80binstruct / ollama fallback + 4 agent + MCP
  mcp/multiap_mcp.py       # stdio MCP 工具服务（复用 src/tools、validator、profile、state_client）
  workspaces/<agent>/      # 各 agent 的 IDENTITY/SOUL/AGENTS/TOOLS.md
```

## 一次性配置
```bash
# 安装 CLI 后执行；脚本会写 profile 配置 + 注册 MCP（生成新 token）
npm install -g openclaw
bash openclaw/setup.sh
```

## 运行（需先有状态服务器在喂数）
```bash
# 1) 启动状态服务器（mock 允许）
python3 state_server/server.py --allow-mock &
# 2) 喂入场景（mock 曲线喂数器，保持状态新鲜）— Stage 4 由 run_openclaw.py 封装
# 3) 驱动协商（默认要求 profile 已配置 qwen80binstruct）
python3 run_openclaw.py --scene joint

# 或直接触发 coordinator
openclaw --profile multiap agent --local --agent coordinator \
  -m "开始协商，请直接调用 run_fast_negotiation 控制总耗时" --json
```

## 当前本机验收状态
- `openclaw` CLI 已安装到 `/opt/homebrew/bin/openclaw`，版本 `2026.6.8`。
- `multiap` profile 已写入 `~/.openclaw-multiap/openclaw.json`，`config validate` 通过。
- `multiap-tools` MCP 使用项目 `.venv`，并配置 `OPENCLAW_BIN=/opt/homebrew/bin/openclaw`、`requestTimeoutMs=600000`，避免长协商在默认 60s 超时。
- 确定性验收已覆盖 `noop/sr/edca/joint`；真实 OpenClaw `edca` smoke 已确认 coordinator 调用 MCP、AP 完成广播/提案/投票/最终 JSON。完整 `sr/edca/joint` 真实收敛仍需作为长链路冒烟继续跑。

## 进度
- [x] Stage 0 OpenClaw 跑通 agent（默认 qwen80binstruct，ollama/qwen3:14b 作为 fallback）
- [x] Stage 1 MCP 工具服务：状态/计算/验算/下发工具，agent 真实调用且结果与 Python 一致
- [x] Stage 2 移植 ap1/ap2/ap3 协商提示词
- [x] Stage 3 coordinator 阶段级触发（run_fast_negotiation，避免逐句调度）
- [x] Stage 4 确定性迁移验收：`noop/sr/edca/joint`、JSONL、执行下发、观测 Validator、Mock 曲线注入路径已接入
- [ ] Stage 4 真实 OpenClaw 冒烟：`edca` 已跑到 AP 最终 JSON；仍需完整跑完 `sr/edca/joint` 三场景收敛
- [x] Stage 5 PPIO/ollama profile 切换、提案注入、终止上限和回归测试
- [ ] Stage 5 README/docs 全量更新，决定旧 Python orchestrator/agent 去留
