# 纯 OpenClaw 架构（迁移中）

把 Multi-AP 协商系统的**托管层**与**编排层**都交给 OpenClaw：
- 托管：`coordinator / ap1 / ap2 / ap3` 作为隔离的 OpenClaw agent，跑在本机 ollama 上。
- 编排：`coordinator`（LLM）通过工具驱动四阶段协商（广播→提案→投票→验收→下发），
  取代原 `src/orchestrator.py` 的 Python 编排。
- 确定性逻辑（Co-SR/Co-EDCA 计算、Validator、状态读取、下发）保留为 Python，
  经 **MCP 工具服务**（`openclaw/mcp/multiap_mcp.py`）暴露给 agent 调用，结果与现有实现一致。

所有配置在隔离 profile `multiap`（`~/.openclaw-multiap/`），不影响用户默认 profile。

## 目录
```
openclaw/
  setup.sh                 # 在 multiap profile 下配置 ollama provider + 4 agent + MCP
  mcp/multiap_mcp.py       # stdio MCP 工具服务（复用 src/tools、validator、profile、state_client）
  workspaces/<agent>/      # 各 agent 的 IDENTITY/SOUL/AGENTS/TOOLS.md
```

## 一次性配置
```bash
bash openclaw/setup.sh                       # 写 profile 配置 + 注册 MCP（生成新 token）
```

## 运行（需先有状态服务器在喂数）
```bash
# 1) 启动状态服务器（mock 允许）
python3 state_server/server.py --allow-mock &
# 2) 喂入场景（mock 曲线喂数器，保持状态新鲜）— Stage 4 由 run_openclaw.py 封装
# 3) 驱动协商（Stage 3 完成后）
OLLAMA_API_KEY=ollama-local openclaw --profile multiap agent --local --agent coordinator -m "开始协商" --json
```

## 进度
- [x] Stage 0 OpenClaw 跑通 agent（ollama/qwen3:14b，embedded）
- [x] Stage 1 MCP 工具服务：状态/计算/验算/下发工具，agent 真实调用且结果与 Python 一致
- [ ] Stage 2 移植 ap1/ap2/ap3 协商提示词
- [ ] Stage 3 coordinator 协议编排（ask_ap + 确定性辅助工具）
- [ ] Stage 4 两种 mock 复现 sr/edca/joint
- [ ] Stage 5 PPIO 模型 / 鲁棒性 / 文档 / 测试
