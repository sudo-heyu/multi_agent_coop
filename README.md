# Multi-AP 协商系统（纯 OpenClaw 架构）

多台 Wi-Fi AP 通过 LLM Agent 自主协商，协调发射功率（Co-SR）和 MAC 退避参数（Co-EDCA），提升整体网络性能。

**架构**：托管层与编排入口均由 **OpenClaw** 运行——`coordinator / ap1 / ap2 / ap3` 是各自独立 workspace/session 的 OpenClaw agent；确定性计算/验算/状态/下发逻辑以 **MCP 工具**暴露给 agent 调用。

**拓扑**：DGX Spark（运行 OpenClaw + 模型 provider + 状态服务器）+ 3 台香蕉派 AP。

---

## 架构总览

```
┌──────────────────────────────────────────────────────────────┐
│ OpenClaw（隔离 profile: multiap，~/.openclaw-multiap/）        │
│                                                              │
│  托管层 agents：coordinator / ap1 / ap2 / ap3                 │
│    模型：PPIO qwen80binstruct（默认）/ ollama qwen3:14b（回退）│
│                                                              │
│  coordinator（LLM，阶段级入口）                               │
│    └─ 调用 MCP 工具 run_fast_negotiation 一次性推进协议        │
│       （不逐句选发言人，控制时延）                            │
│                                                              │
│  MCP 工具服务 multiap-tools（openclaw/mcp/multiap_mcp.py）：   │
│    get_latest_ap_states / analyze_sr_interference /          │
│    compute_sr_feasible_ranges / select_sr_concurrent_groups /│
│    evaluate_sr_candidate / rank_sr_candidates /              │
│    validate_edca_proposal（AP 可调用）                        │
│    run_fast_negotiation / validate_decision / push_decision  │
│      （仅 coordinator，AP 经 per-agent tools.deny 禁用）       │
└──────────────────────────────────────────────────────────────┘
        │ MCP stdio                       │ openclaw agent --local
        ▼                                 ▼
  src/tools{sr,edca} · validator ·   state_server（Flask）
  profile · state_client            ← reporter / mock 喂数器 / 预设场景
```

编排「机制层」（阶段指令、驱动 AP、计票、反提案接管、Validator 重试与终止）实现在
`openclaw/mcp/orchestration.py` 的 `structured_relay`，由 `run_fast_negotiation` 工具内部调用。

---

## 快速开始

### 依赖

```bash
pip install -r requirements.txt          # flask requests python-dotenv matplotlib mcp
npm install -g openclaw                   # OpenClaw CLI

# 模型：二选一
#  · PPIO 云端 qwen80binstruct（默认、更稳）——在仓库 .env 写 PPIO_API_KEY=...
#  · 本机 ollama 回退——ollama pull qwen3:14b
```

### 一次性配置（隔离 profile，不影响用户默认 profile）

```bash
bash openclaw/setup.sh
# 写 ~/.openclaw-multiap/openclaw.json（providers + 4 agent + 默认模型 + per-agent 工具限制）
# 注册 MCP 工具服务 multiap-tools，并执行 config validate
```

> 默认模型：检测到 `.env` 里有 `PPIO_API_KEY` 时用 PPIO `qwen80binstruct`，否则回退 `ollama/qwen3:14b`。

### 一、Mock 模式（无需真实 AP，直接跑仿真场景）

```bash
# 默认 joint 场景；--scene 可选 sr / edca / joint
python run_openclaw.py --scene edca

# 常用开关
python run_openclaw.py --scene sr --no-academic-plot --no-dashboard --exit-after-run
#   --no-feeder        只推一帧（不持续喂曲线，长协商可能因状态过期失败，演示曲线请勿加）
#   --direct-relay     调试用：绕过 coordinator，直接运行阶段接力（仍经 OpenClaw AP agent）
#   --require-qwen80b  强制校验 profile 默认模型为 qwen80binstruct
```

`run_openclaw.py` 会准备场景数据、拉起状态服务器/Dashboard/学术曲线，再触发
`coordinator` 调用 `run_fast_negotiation` 完成一轮协商，协商后把决策注入喂数器，曲线体现改善。

### 二、真实 AP 模式

```bash
# 1) 状态服务器（DGX Spark，启动一次）
python state_server/server.py
#    默认拒收 source=mock/generated 等生成数据；浏览器开 http://localhost:5001 看实时状态

# 2) 各香蕉派 AP 上报（--ap-id 换成 ap1/ap2/ap3）
python state_server/reporter.py --ap-id ap1 --server http://<DGX_IP>:5001

# 3) 触发协商（推送决策到执行端点）
python run_openclaw.py --server http://localhost:5001 \
  --ap-endpoints ap1=192.168.1.1:5002,ap2=192.168.1.2:5002,ap3=192.168.1.3:5002
#  或 --ap-config ap_endpoints.json（默认自动读取仓库根 ap_endpoints.json）
```

也可直接触发 coordinator（不经薄启动器）：

```bash
OLLAMA_API_KEY=ollama-local openclaw --profile multiap agent --local --agent coordinator \
  -m "开始协商，请直接调用 run_fast_negotiation 控制总耗时" --json
```

---

## 三个仿真场景

| 场景 | `--scene` | 触发条件 | 协商路径 |
|------|-----------|----------|----------|
| Co-SR | `sr` | 邻居 RSSI 偏强（强干扰） | 降低发射功率 |
| Co-EDCA | `edca` | 邻居弱、优先级/QoS 分化 | 调整 CWmin/CWmax/AIFSN |
| 联合 | `joint` | 高功率 + 优先级分化同时成立 | 联合调整，或先处理主导问题（由实时证据决定） |

路径由提案 AP 基于实时状态与工具验算自主选择，不按 AP 编号或固定业务身份预设。
场景定义见 `openclaw/scenes.py`（`MOCK_SCENE_SR/EDCA/JOINT`）。

---

## 协商流程（四阶段，由 `structured_relay` 编排）

```
阶段 1 广播   ap1→ap2→ap3 依次播报自身实测数据（只报己方数据 + 本机扫描的邻居 RSSI）
    ↓
阶段 2 提案   首轮固定由 ap1 发起、自主选路；提案前必须 get_latest_ap_states，
              Co-SR 先 analyze_sr_interference→select_sr_concurrent_groups，提交前自检
    ↓
阶段 3 投票   非提案 AP 逐一表态 同意/弃权/反对；反对者当场给反提案并接管为新提案方
              （反提案 JSON 解析失败时触发一次「修复轮」补纯 JSON）
    ↓
（如未通过）  Validator 未过则写回原因，从 ap1 重提案，最多 3 轮
    ↓
阶段 4 决策   提案方输出最终 JSON → 确定性 Validator 验算 → 通过则下发执行
```

终止保证：重投上限 `MAX_VOTE_ROUNDS=3`、验证重试 3、单轮最大发言 30，不收敛时干净退出。

### Agent 可调用的 MCP 工具

| 工具名 | 阶段 | 作用 |
|--------|------|------|
| `get_latest_ap_states` | 提案 / 投票 | 获取全部 AP 最新参数状态；提案或投票前必须先调用 |
| `analyze_sr_interference` | Co-SR 提案 | 分析强/中等干扰链路、主要干扰源和受害 AP |
| `compute_sr_feasible_ranges` | Co-SR 提案 | 计算每个 AP 的 TX Power 可行区间 |
| `select_sr_concurrent_groups` | Co-SR 提案 | 选择空间复用并发组并给出组内推荐功率 |
| `evaluate_sr_candidate` | Co-SR 提案 / 投票 | 评估候选功率是否满足 CCA/SINR/STA-RSSI 约束 |
| `rank_sr_candidates` | Co-SR 提案 | 对多个候选功率方案按目标排序 |
| `validate_edca_proposal` | 提案 / 投票 | 验算 EDCA 参数合法性、优先级单调性与拥塞匹配度 |

coordinator 专用（AP 经 per-agent `tools.deny` 禁用）：`run_fast_negotiation`、`validate_decision`、`push_decision`。
工具实现见 `openclaw/mcp/multiap_mcp.py`，复用 `src/tools/`、`src/validator.py` 等保留的确定性 Python。

---

## 项目结构

```
.
├── run_openclaw.py               # 薄启动器：准备场景 → 拉起服务器/Dashboard/曲线 → 触发 coordinator
├── openclaw/
│   ├── setup.sh                  # 配置隔离 profile multiap（providers + 4 agent + 工具限制 + MCP 注册）
│   ├── scenes.py                 # 三套 mock 场景 + 状态服务器/Dashboard/学术曲线启动器
│   ├── mcp/
│   │   ├── multiap_mcp.py        # stdio MCP 工具服务（暴露计算/验算/状态/编排/下发工具）
│   │   ├── orchestration.py      # 编排机制层：四阶段 structured_relay、驱动 AP、计票、反提案
│   │   ├── proposal_utils.py     # 提案/JSON/策略推断纯函数
│   │   └── tool_console.py       # 工具调用富文本 formatter（--direct-relay 展示）
│   └── workspaces/<agent>/       # 各 agent 的 IDENTITY/SOUL/AGENTS/TOOLS.md
├── state_server/
│   ├── server.py                 # Flask 状态服务器（AP 上报 / MCP 工具读取）
│   ├── reporter.py               # AP 状态上报脚本（部署在香蕉派，或本地 mock）
│   ├── executor.py               # 执行端点：接收决策并下发到硬件
│   ├── mock_feeder.py            # mock 曲线喂数器（持续喂遥测 + 协商后注入决策）
│   └── academic_plot.py          # Matplotlib 学术曲线窗口
├── src/                          # 保留的确定性基础设施/工具（被 MCP 工具复用）
│   ├── tools/{sr,edca}.py        # Co-SR / Co-EDCA 计算与约束验算
│   ├── validator.py              # 确定性 Validator：物理约束最终验算
│   ├── profile.py                # 状态规范化 + 字段白名单
│   ├── state_client.py           # 读取状态服务器（含过期检查）
│   ├── logger.py                 # 结构化 JSONL 日志
│   └── console_style.py          # 彩色终端输出
├── dashboard/                    # Flask + SSE 实时协商对话 Dashboard
├── logs/                         # 每次运行生成一个 session_*.jsonl
└── docs/                         # 设计文档（docs/openclaw/ 为 OpenClaw 自身参考文档）
```

---

## 日志

每次运行在 `logs/` 生成一个 JSONL 文件，每行一个事件
（`session_start` / `phase_start` / `agent_speak` / `tool_call` / `vote` / `round_result` /
`final_decision` / `validation_result` / `executor_apply` / `session_end`），供 Dashboard 可视化或离线分析。

---

## 物理约束（Validator 检查项）

**Co-SR**
- `TX Power` ∈ [1, 23] dBm，功率调整量相对协商前为整数 dB
- CCA（邻居接收信号）/ SINR / 降功率后 STA RSSI 约束（由计算工具保证）

**Co-EDCA**
- `CWmin` ∈ [3, 1023]，`CWmax` ∈ [7, 1023]，`AIFSN` ∈ [1, 15]，`CWmax > CWmin`
- 优先级单调性：`high.CWmin ≤ medium ≤ low`（AIFSN 同理）

---

## 测试

```bash
.venv/bin/python -m unittest discover -s tests
# 覆盖：三场景结构等价、反提案修复轮、MCP 提案回填、状态服务器、Dashboard、学术曲线
```
