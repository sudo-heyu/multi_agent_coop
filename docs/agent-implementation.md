# Agent 实现说明

## 现行架构

系统由两类运行时协作完成：

- OpenClaw 托管 `ap1 / ap2 / ap3` 三个独立 agent，负责读取完整对话、调用 MCP 工具并生成广播、提案和投票内容。
- Python 负责确定性机制：`run_openclaw.py` 是默认入口；`openclaw/mcp/orchestration.py` 的 `structured_relay` 控制阶段、计票、反提案接管、重试、Validator 和执行下发。

`coordinator` 仍存在于隔离 profile 中，但已不是默认入口。只有显式传入 `--use-coordinator` 时，才由 coordinator 调用 MCP 工具 `run_fast_negotiation`；该工具内部仍运行同一个 `structured_relay`。

## 运行链路

```text
run_openclaw.py
  ├─ 检查常驻 state server / OpenClaw gateway / Dashboard
  ├─ 等待外部持续上报（real：香蕉派 reporter / ns3：ns3_bridge）
  ├─ 直接调用 structured_relay
  │    ├─ drive_ap → OpenClaw gateway → ap1/ap2/ap3
  │    ├─ AP → multiap-tools MCP → 状态/Co-SR/Co-EDCA 工具
  │    ├─ validate_decision 确定性验收
  │    └─ 可选 POST /apply 到各 AP executor
  └─ SessionLogger → JSONL，并经 Dashboard /push → SSE
```

默认路径不会启动 coordinator，也不会临时启动 state server、gateway、Dashboard 或学术曲线窗。这些服务由 `openclaw/serve.sh` 管理并在多次协商间复用。

## AP 回合

`drive_ap(ap_id, instruction)` 的行为如下：

1. 把共享 transcript 与当前阶段指令合并为本次 message。
2. 为每次发言生成全新的随机 session-id，避免会话锁和历史重复累积。
3. gateway 在线时执行 `openclaw --profile multiap agent --agent <ap> ...`；连接失败时回退 `--local`。
4. tail OpenClaw session/raw-stream JSONL，将文本增量和工具结果转发到终端、日志与 Dashboard。
5. 空回复或进程级失败最多重试 3 次。

AP 可调用 `get_latest_ap_states`、Co-SR 分析/候选工具以及 `validate_edca_proposal`。通过 per-agent `tools.deny`，AP 不能调用 coordinator 专用的 `run_fast_negotiation`。

## 协商机制

1. 广播：三台 AP 并发生成广播，系统按 ap1、ap2、ap3 顺序记录和展示；每台只陈述自己的状态。
2. 触发判断：`determine_strategy` 返回 `co_sr`、`co_edca`、`joint` 或 `noop`，作为提案快速路径提示。
3. 提案：首轮由 ap1 发起。AP 获取最新状态、调用必要工具并输出候选 JSON。
4. 投票：非提案 AP 逐一投票；反对者必须给出反提案并接管。反提案无法解析时允许一次纯 JSON 修复轮。
5. 决策与验收：全票通过后直接采用已通过的提案作为最终决策，不再增加一次 LLM 决策回合；Validator 验收通过后才允许下发。

Validator 最多重试 3 轮，单轮最多 30 次发言；达到上限后返回明确的失败 outcome。

## 目录职责

| 路径 | 职责 |
|---|---|
| `run_openclaw.py` | 默认启动器、服务检查、mock feeder、日志和结果输出 |
| `openclaw/serve.sh` | 常驻 state/gateway/Dashboard/plot 生命周期 |
| `openclaw/mcp/orchestration.py` | 阶段机制、OpenClaw AP 驱动、Validator、下发 |
| `openclaw/mcp/multiap_mcp.py` | AP 工具及兼容 coordinator 工具的 MCP 服务 |
| `openclaw/workspaces/ap*/` | AP 身份、协议和工具提示词 |
| `src/tools/` | Co-SR / Co-EDCA 确定性计算 |
| `src/validator.py` | 最终决策确定性验收 |

## 运行

```bash
# 一次性配置
MULTIAP_PY="$PWD/.venv/bin/python" bash openclaw/setup.sh

# 每次启动/检查常驻服务
bash openclaw/serve.sh start
bash openclaw/serve.sh status

# 默认路径
.venv/bin/python run_openclaw.py --mode ns3 --scene edca

# 兼容对比路径（额外经过 coordinator LLM）
.venv/bin/python run_openclaw.py --mode ns3 --scene edca --use-coordinator
```

## 验收

```bash
.venv/bin/python -m unittest discover -s tests
```

当前确定性测试为 48 项，新增覆盖 SQLite v5 Episodic Memory、领域特征、拓扑隔离、相似排序和提案案例注入；其余覆盖 Session Memory、安全恢复、action journal、executor 幂等、状态服务、Dashboard 和三种策略。
