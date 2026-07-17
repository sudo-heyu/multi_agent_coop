# Agent 实现记录（OpenClaw）

## 概述

托管层的 `coordinator / ap1 / ap2 / ap3` 是**真正的 OpenClaw agent**，各自独立 workspace/session，
模型经 OpenClaw 的 provider 机制调用（默认 PPIO `qwen80binstruct`，无 PPIO key 时回退 ollama `qwen3:14b`）。
编排「机制层」（阶段指令、驱动 AP、计票、反提案、Validator 重试与终止）在
`openclaw/mcp/orchestration.py` 的 `structured_relay`，由 MCP 工具 `run_fast_negotiation` 内部调用。
确定性计算/验算/状态/下发逻辑保留为 Python，经 MCP 工具服务 `multiap-tools` 暴露给 agent。

---

## 目录结构

```
openclaw/
├── setup.sh                  # 在隔离 profile multiap 下写配置 + 注册 MCP
├── scenes.py                 # 通用执行端点解析 helper
├── mcp/
│   ├── multiap_mcp.py        # stdio MCP 工具服务
│   ├── orchestration.py      # structured_relay 四阶段编排机制
│   ├── proposal_utils.py     # 提案/JSON/策略推断纯函数
│   └── tool_console.py       # 工具调用富文本 formatter
└── workspaces/<agent>/
    ├── IDENTITY.md           # 身份：AP 编号、感知方式
    ├── SOUL.md               # 角色定位：诚实、克制、协作
    ├── AGENTS.md             # 行为准则与协商协议约束
    └── TOOLS.md              # 可用 MCP 工具说明
```

每个 agent 的系统提示词由其 workspace 下的 `IDENTITY/SOUL/AGENTS/TOOLS.md` 组成，由 OpenClaw 运行时注入。

---

## 模块说明

### AP agent（OpenClaw `--agent ap{1,2,3}`）

AP 不再是进程内的 Python 类，而是独立的 `openclaw agent --local` 进程。
`orchestration.py` 的 `drive_ap(ap_id, instruction)` 负责一次 AP 回合：

- 把共享 `transcript`（全部历史发言）+ 当前阶段 `instruction` 作为 message 传入；
- 每次发言使用全新随机 session-id（无状态发言，避免持久 session 锁/历史累积）；
- 底层执行 `openclaw --profile multiap agent --local --agent <ap> --thinking off --message <msg> --json`；
- 云端/本地偶发空回复（payloads=0）时自动重试（默认 3 次）。

AP 在回合内可自主调用 MCP 计算/验算工具（`get_latest_ap_states`、`analyze_sr_interference`、
`select_sr_concurrent_groups`、`evaluate_sr_candidate`、`validate_edca_proposal` 等）。
通过 per-agent `tools.deny`，AP 被禁用 coordinator 专用工具（`run_fast_negotiation`）。

### coordinator agent（OpenClaw `--agent coordinator`）

coordinator 是阶段级入口（LLM）：收到「开始协商」后只调用一次 MCP 工具 `run_fast_negotiation`，
由工具内部批量驱动广播→提案→投票→决策→验收，**不逐句选择发言人**，以控制时延。

### 编排机制 `structured_relay`（`openclaw/mcp/orchestration.py`）

复刻原确定性四阶段控制流，把「发言」换成 `drive_ap`：

1. **广播**：`ap1→ap2→ap3` 依次播报自身实测数据；
2. **触发判断**：`determine_strategy` 给出策略提示，`noop` 时直接干净退出；
3. **提案**：首轮固定 ap1 自主选路，提交前自检（`evaluate_sr_candidate`/`validate_edca_proposal`）；
4. **投票**：非提案 AP 逐一 同意/弃权/反对；反对者给反提案并 `promote_counter` 接管；
   反提案 JSON 解析失败时触发一次「修复轮」`run_repair_counter` 补纯 JSON；
5. **决策 + 验收**：全票通过后输出最终 JSON，确定性 `validate_decision` 验算，通过则 `_push_decision` 下发。

终止保证：验证重试上限 3、单轮最大发言 30，不收敛时返回明确 outcome 干净退出。

---

## 提示词设计要点

| 文件 | 内容 | 作用 |
|---|---|---|
| `IDENTITY.md` | AP 编号、感知方式 | 身份锚定，防止角色漂移 |
| `SOUL.md` | 诚实、克制、协作、语气风格 | 控制发言风格和长度 |
| `AGENTS.md` | 四阶段协议完整规则 | 行为约束的核心 |
| `TOOLS.md` | 可用 MCP 工具的位置/参数/返回/调用时机 | 工具使用规范 |

阶段指令文本由 `orchestration.py` 的 `broadcast_instruction / propose_instruction /
vote_instruction / final_instruction` 构造，与协议约束一致。

---

## 验收结果

- 确定性套件全绿：三场景结构等价、反提案修复轮、MCP 提案回填等。
- 真实 OpenClaw + PPIO `qwen80binstruct` 场景端到端跑通、Validator 通过：
  `edca→co_edca`、`sr→co_sr`；两类证据同时出现时按主导问题选择其中一种单一策略。

---

## 运行方式

```bash
bash openclaw/setup.sh                    # 一次性配置隔离 profile + 注册 MCP
python run_openclaw.py --data-source ns3   # 从 state server 读取 ns-3 遥测并触发协商

# 或直接触发 coordinator
OLLAMA_API_KEY=ollama-local openclaw --profile multiap agent --local --agent coordinator \
  -m "开始协商，请直接调用 run_fast_negotiation 控制总耗时" --json
```
