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

## 运行（ns-3 默认闭环）
```bash
# 先启动 state/gateway 等常驻服务
bash openclaw/serve.sh start

# 默认托管 live ns-3：读 TELEMETRY，最终决策经 stdin APPLY 写回 ns-3
.venv/bin/python run_openclaw.py --data-source ns3 \
  --ns3-scenario line --ns3-business-profile live_bulk \
  --no-dashboard --no-academic-plot

# 或直接触发 coordinator
openclaw --profile multiap agent --local --agent coordinator \
  -m "开始协商，请直接调用 run_fast_negotiation 控制总耗时" --json
```

外部 ns-3 bridge 只作为调试模式保留：`run_openclaw.py --data-source ns3 --ns3-external`，配合 `state_server/ns3_bridge.py` 转发真实 ns-3 输出。该 bridge 不生成、不扰动 QoS。

## 后台常驻服务（serve.sh，一条命令全开）
OpenClaw 已为 multiap 注册 launchd 网关服务 `ai.openclaw.multiap`（端口 18789，RunAtLoad+KeepAlive，
本身即长期服务）。serve.sh 把 state server / gateway / Dashboard / 学术曲线窗绑成一条命令，run_openclaw 强制复用：
```bash
bash openclaw/serve.sh start    # 一条命令全开：state(5001) + gateway(18789) + Dashboard(5050) + 曲线窗
bash openclaw/serve.sh status   # 四者状态
bash openclaw/serve.sh stop     # 停曲线/State/Dashboard；launchd 托管的 gateway 不强停
bash openclaw/serve.sh restart  # 改过 setup.sh/MCP 注册后重载 gateway
```
- **先 `serve.sh start` 再跑 run_openclaw**：默认路径强制检测 state/gateway/Dashboard 在线，不在线报错提示先 `serve.sh start`，不再临时起兜底。
- Dashboard 常驻是独立进程，run_openclaw 经 HTTP `POST /push` 把会话事件推给它再 SSE 广播——常驻 Dashboard 也有实时对话流。
- 学术曲线窗（matplotlib）也常驻，run_openclaw 复用省每次冷启动；无桌面自动跳过。
- 端口取自 `gateway.port`（默认 18789）；serve.sh 优先复用 launchd，缺失才 nohup 兜底。`drive_ap` 在线走热 gateway，连接失败回退 `--local`。
- 提速主要省冷启动/预热，**PPIO 推理时长不变**。

## 验收状态（迁移已完成）
- `openclaw` CLI `2026.6.8`；`multiap` profile 写入 `~/.openclaw-multiap/openclaw.json`，`config validate` 通过。
- `multiap-tools` MCP 使用项目 `.venv`，配置 `OPENCLAW_BIN`、`requestTimeoutMs=600000`，避免长协商超时。
- `run_openclaw.py` 启动时校验 `multiap-tools` MCP 必须指向当前仓库，避免常驻 gateway 误用旧目录代码。
- ap1/ap2/ap3 经 per-agent `tools.deny` 禁用 coordinator 专用工具（run_fast_negotiation）。
- 确定性套件全绿（三场景结构等价、反提案修复轮、MCP 回填等）。
- 真实 OpenClaw + PPIO `qwen80binstruct` 场景端到端跑通、Validator 通过：
  `edca→co_edca`、`sr→co_sr`；两类证据同时出现时按主导问题选择其中一种单一策略。
- 原 Python 编排架构（orchestrator/agent/registry/run.py 等）已删除，OpenClaw 为唯一运行时。
