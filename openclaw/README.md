# 纯 OpenClaw 架构（唯一运行时）

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
# run_openclaw.py 一站式：准备场景 + 状态服务器 + 连续喂数器 + Dashboard/曲线 + 触发 coordinator
python run_openclaw.py --scene joint

# 或直接触发 coordinator
openclaw --profile multiap agent --local --agent coordinator \
  -m "开始协商，请直接调用 run_fast_negotiation 控制总耗时" --json
```

## 后台常驻服务（serve.sh，加速冷启动）
OpenClaw 已为 multiap 注册 launchd 网关服务 `ai.openclaw.multiap`（端口 18789，RunAtLoad+KeepAlive，
本身即长期服务）。serve.sh 把它与 state server 绑成一条命令：
```bash
bash openclaw/serve.sh start    # 起 state server(5001) + 确保 multiap gateway(18789) 在线
bash openclaw/serve.sh status   # 两者状态
bash openclaw/serve.sh stop     # 停 state server；launchd 托管的 gateway 不强停
```
- 端口取自 `gateway.port`（默认 18789，与 launchd 服务一致）；serve.sh 优先复用 launchd，缺失才 nohup 兜底。
- 起了 gateway 后，`drive_ap` 探测到在线即自动让 AP 回合走热 gateway（省每回合 runtime/MCP 冷启动），
  离线回退 `--local`；coordinator 入口仍 `--local`（避免 MCP 实例重入死锁）。
- 提速主要省冷启动/预热，**PPIO 推理时长不变**。

## 验收状态（迁移已完成）
- `openclaw` CLI `2026.6.8`；`multiap` profile 写入 `~/.openclaw-multiap/openclaw.json`，`config validate` 通过。
- `multiap-tools` MCP 使用项目 `.venv`，配置 `OPENCLAW_BIN`、`requestTimeoutMs=600000`，避免长协商超时。
- ap1/ap2/ap3 经 per-agent `tools.deny` 禁用 coordinator 专用工具（run_fast_negotiation/validate_decision/push_decision）。
- 确定性套件全绿（三场景结构等价、反提案修复轮、MCP 回填等）。
- 真实 OpenClaw + PPIO `qwen80binstruct` 三场景端到端跑通、Validator 通过：
  `edca→co_edca`、`sr→co_sr`、`joint→co_sr`（按规范「先处理主导问题」，基于实时证据）。
- 原 Python 编排架构（orchestrator/agent/registry/run.py 等）已删除，OpenClaw 为唯一运行时。
