# Multi-AP 协商系统

多台 Wi-Fi AP 通过 LLM Agent 自主协商，协调发射功率（Co-SR）和 MAC 退避参数（Co-EDCA），提升整体网络性能。

**拓扑**：DGX Spark（运行 Ollama + 状态服务器 + orchestrator）+ 3 台香蕉派 AP

---

## 快速开始

### 依赖

```bash
pip install requests flask
# Ollama 已在本机运行，并拉取了所需模型
ollama pull qwen3:14b
```

### 一、Mock 模式（无需真实 AP，直接跑仿真场景）

```bash
# 联合场景（默认）：同时触发 Co-SR + Co-EDCA
python run.py --mock

# 仅 Co-SR 场景：三台 AP 功率不对称，AP1 高位干扰邻居
python run.py --mock --scene sr

# 仅 Co-EDCA 场景：AP1 信道严重拥塞
python run.py --mock --scene edca

# 指定模型
python run.py qwen3.6:27b --mock --scene joint
```

### 二、真实 AP 模式

**第一步：启动状态服务器**（DGX Spark 上，只需启动一次）

```bash
python state_server/server.py
# 监听 0.0.0.0:5001
# 默认真实上报模式：拒收 source=mock/generated/synthetic 等生成数据
# 浏览器打开 http://localhost:5001 可查看实时 AP 状态
```

**第二步：各香蕉派 AP 上报数据**

```bash
# 在香蕉派上运行，--ap-id 替换为对应 ID（ap1 / ap2 / ap3）
python state_server/reporter.py --ap-id ap1 --server http://<DGX_IP>:5001

# 本地 mock 上报仅用于测试；状态服务器需显式用 --allow-mock 启动
python state_server/server.py --allow-mock
python state_server/reporter.py --mock --all
```

**第三步：运行协商**

```bash
# run.py 检测到服务器未启动时会自动拉起 server.py
python run.py --server http://localhost:5001
```

---

## 三个仿真场景

| 场景 | `--scene` | 触发条件 | 协商路径 |
|------|-----------|----------|----------|
| Co-SR | `sr` | AP1 邻居 RSSI > −70 dBm（强干扰） | 降低发射功率 |
| Co-EDCA | `edca` | AP1 信道占用 > 60% 且重传率 > 15% | 调整 CWmin/CWmax/AIFSN |
| 联合 | `joint` | 两类指标同时超阈值 | 同时调整功率和 MAC 参数 |

---

## 协商流程

```
阶段 1 广播   各 AP 播报自身实测数据（TX Power、RSSI、信道占用等）
    ↓
阶段 2 提案   状况最差的 AP 先获取最新状态，再调用计算工具生成参数调整方案
    ↓
阶段 3 投票   其余 AP 先获取最新状态，再调用验算工具验证参数约束后表态
    ↓
（如未通过）  提案方根据反馈修订，最多 3 轮
    ↓
阶段 4 决策   提案方输出最终 JSON → Validator 确定性验算 → 写入日志
```

### Agent 工具调用

每个 AP Agent 拥有多个可调用工具（定义在 `src/tools/registry.py`，说明见各 `agents/ap*/TOOLS.md`）：

| 工具名 | 阶段 | 作用 |
|--------|------|------|
| `get_latest_ap_states` | 提案 / 投票 | 获取全部 AP 最新参数状态；提案或投票时必须先调用 |
| `analyze_sr_interference` | Co-SR 提案 | 分析强/中等干扰链路、主要干扰源和受害 AP |
| `compute_sr_feasible_ranges` | Co-SR 提案 | 计算每个 AP 的 TX Power 可行区间 |
| `evaluate_sr_candidate` | Co-SR 提案 / 投票 | 评估候选功率是否满足 CCA/SINR/STA-RSSI 约束 |
| `rank_sr_candidates` | Co-SR 提案 | 对多个候选功率方案按目标排序 |
| `validate_edca_proposal` | 提案 / 投票 | 验算提案 EDCA 参数是否合法，并评估拥塞匹配度、碰撞风险和公平性 |

Agent 通过 Ollama tool_calls 机制真实调用工具，而非依赖 prompt 注入。

---

## 项目结构

```
.
├── run.py                        # 入口：解析参数、启动服务器（如未运行）、触发协商
├── state_server/
│   ├── server.py                 # Flask 状态服务器（AP 上报 / orchestrator 读取）
│   └── reporter.py               # AP 状态上报脚本（部署在香蕉派，或本地 mock）
├── src/
│   ├── agent.py                  # APAgent：封装 Ollama 调用，实现 tool calling 循环
│   ├── orchestrator.py           # 协商编排：4 阶段流程控制
│   ├── validator.py              # 确定性 Validator：物理约束最终验算
│   ├── logger.py                 # 结构化 JSONL 日志
│   ├── console_style.py          # 彩色终端输出
│   └── tools/
│       ├── sr.py                 # Co-SR 计算：干扰矩阵、连续功率优化、约束验算
│       ├── edca.py               # Co-EDCA 计算：拥塞分级、参数映射、合法性验证
│       └── registry.py           # 工具注册中心：JSON Schema + 执行器工厂
├── agents/
│   ├── ap{1,2,3}/
│   │   ├── IDENTITY.md           # AP 身份定义
│   │   ├── SOUL.md               # 行为原则
│   │   ├── AGENTS.md             # 协商行为规范（广播/提案/投票/决策格式）
│   │   └── TOOLS.md              # 可用工具说明（位置、参数、返回值、调用时机）
│   └── validator/                # Validator 说明文档（不走 LLM）
├── logs/                         # 每次运行生成一个 session_*.jsonl
└── docs/                         # 设计文档
```

---

## 日志

每次运行在 `logs/` 生成一个 JSONL 文件，记录完整会话：

```
session_20260520_120000_a1b2c3d4.jsonl
```

每行一个事件（`session_start` / `strategy_decided` / `phase_start` / `tool_call` / `agent_speak` / `vote` / `round_result` / `final_decision` / `validation_result` / `session_end`），可用于 Dashboard 可视化或离线分析。

---

## 物理约束（Validator 检查项）

**Co-SR**
- `TX Power` ∈ [1, 23] dBm
- CCA（邻居接收信号）< −82 dBm
- SINR ≥ 15 dB
- 降功率后 STA RSSI ≥ −75 dBm

**Co-EDCA**
- `CWmin` ∈ [3, 1023]，`CWmax` ∈ [7, 1023]，`AIFSN` ∈ [1, 15]
- `CWmax > CWmin`
