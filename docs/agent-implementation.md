# AP Agent 实现记录

## 概述

本文档记录第二步的实现结果：基于 Ollama 本地大模型的三 AP 协商系统。
系统不依赖 OpenClaw 安装，采用等价的 Python 实现，用文件夹 + Markdown 管理提示词，用 Python orchestrator 驱动对话流程。

测试时间：2026-05-19  
测试模型：`qwen3:14b`（`think: false`）  
测试场景：场景二 静态 Co-EDCA 仿真  
结果：一轮投票全票通过，输出合法 JSON，协商结束

---

## 目录结构

```
multi-ap-coop/
├── agents/
│   ├── ap1/
│   │   ├── IDENTITY.md   # 身份：AP编号、感知方式
│   │   ├── SOUL.md       # 角色定位：诚实、克制、协作
│   │   └── AGENTS.md     # 行为准则：三步协商协议的完整约束
│   ├── ap2/              # 结构同 ap1，IDENTITY.md 不同
│   └── ap3/              # 结构同 ap1，IDENTITY.md 不同
├── src/
│   ├── __init__.py
│   ├── agent.py          # APAgent 类
│   └── orchestrator.py   # NegotiationOrchestrator 类
└── run.py                # 触发脚本（场景二 mock 数据）
```

---

## 模块说明

### APAgent（`src/agent.py`）

每个 AP agent 对应一个 `APAgent` 实例。

**初始化**：读取 `agents/<ap_id>/` 下的三个 Markdown 文件，按 `IDENTITY.md → SOUL.md → AGENTS.md` 顺序拼接为系统提示词（system prompt），用 `---` 分隔。

**核心方法 `speak(conversation_log, instruction)`**：
- `conversation_log`：共享对话历史，格式为 `[{"speaker": "AP1", "content": "..."}]`
- `instruction`：orchestrator 当前阶段给该 agent 的指令
- 将历史转为文字记录，与 instruction 一起作为 user message
- 调用 Ollama `/api/chat`，参数 `think: false, stream: false`，超时 180s
- 返回模型回复的纯文本

**模型调用参数**：
```python
{
    "model": "qwen3:14b",   # 可切换为 qwen3.6:27b
    "think": False,          # 禁用 thinking，避免超时
    "stream": False,
    "messages": [
        {"role": "system", "content": <系统提示词>},
        {"role": "user",   "content": <对话记录 + 当前指令>}
    ]
}
```

---

### NegotiationOrchestrator（`src/orchestrator.py`）

管理三步协商流程，持有三个 `APAgent` 实例和一个共享 `conversation_log`。

**入口 `run(ap_state)`**：
```
ap_state: {"ap1": {指标字典}, "ap2": {...}, "ap3": {...}}
```

**第一阶段 `_phase_broadcast(ap_state)`**：  
依次让 AP1 → AP2 → AP3 广播自身状态。每次调用 `speak()` 时，instruction 中附带该 AP 的状态数据（JSON），要求只播报己方数据。

**第二阶段 `_phase_propose(proposer_id, ap_state)`**：  
由 `_find_worst_ap()` 选出综合得分最高的 AP 作为提案发起方：

```python
score = Data_rate_to_bandwidth_ratio + tx_retries_ratio * 2
```

提案方收到所有 AP 的完整状态，判断协商路径（Co-SR / Co-EDCA / 联合）并给出具体参数值。

**第三阶段 `_phase_vote(proposer_id)`**：  
非提案 AP 逐一验算提案中针对自己的参数，表态同意（✅）或不同意（❌）。  
同意检测逻辑：先判 `不同意/❌`，无则判 `同意/✅`，避免"不同意"被误判为同意。

**重投机制**：最多 `MAX_VOTE_ROUNDS = 3` 轮。有 AP 反对时，要求提案方根据反馈修订，再次投票。

**输出最终决策**：全票同意后，提案方输出合法 JSON + "协商结束"。

---

## 提示词设计要点

三个文件分工：

| 文件 | 内容 | 作用 |
|---|---|---|
| `IDENTITY.md` | AP 编号、感知方式 | 身份锚定，防止角色漂移 |
| `SOUL.md` | 诚实、克制、协作、语气风格 | 控制发言风格和长度 |
| `AGENTS.md` | 三步协议完整规则 | 行为约束的核心 |

**AGENTS.md 的关键约束**：
- 第一阶段：严禁引用其他 AP 数据（模型自发遵守，将邻居 RSSI 字段标注为"不播报（非我数据）"）
- 第二阶段：Co-EDCA 触发条件——重传率 > 15% 且信道占用 > 60%；Co-SR——邻居 RSSI > -70 dBm
- 第三阶段：只验算与自身相关的参数；RSSI 安全下界 -75 dBm；参数范围约束
- 最终 JSON：格式固定，JSON 内不得有注释

---

## 测试结果（场景二 Co-EDCA）

**初始状态**：

| AP | CWmin | AIFSN | 信道占用 | 重传率 | 吞吐量 | 延迟 |
|---|---|---|---|---|---|---|
| AP1 | 3 | 1 | 82% | 31% | 18.4 Mbps | 312 ms |
| AP2 | 7 | 2 | 55% | 12% | 28.7 Mbps | 185 ms |
| AP3 | 15 | 4 | 38% | 5% | 34.1 Mbps | 98 ms |

**协商结果（AP1 提案，一轮全票通过）**：

```json
{
  "AP1": {"strategy": "调整EDCA参数", "CWmin": 15, "CWmax": 63, "AIFSN": 3},
  "AP2": {"strategy": "调整EDCA参数", "CWmin": 7,  "CWmax": 31, "AIFSN": 2},
  "AP3": {"strategy": "调整EDCA参数", "CWmin": 7,  "CWmax": 31, "AIFSN": 4}
}
```

AP1 正确识别 Co-EDCA 路径，AP2/AP3 均完成参数范围验算后同意。

---

## 当前局限

1. **数据来源是 mock**：`run.py` 中的状态数据为硬编码，未对接香蕉派真实上报
2. **无计算工具**：提案的参数值完全由 LLM 生成，无 Co-SR/Co-EDCA 数值计算模块背书
3. **validator 缺失**：投票仅靠字符串检测，无独立的指标核算和安全检测 agent
4. **无日志**：对话内容仅打印到 stdout，未持久化为结构化日志
5. **邻居 RSSI 广播行为待澄清**：第一阶段模型将邻居 RSSI 标注为"不播报"，但邻居 RSSI 是自身观测数据，应当播报；提示词措辞需后续修正

---

## 运行方式

```bash
# 默认模型 qwen3:14b
python run.py

# 指定更大模型
python run.py qwen3.6:27b
```

依赖：`requests`（标准库外唯一依赖），Ollama 服务在 `http://localhost:11434` 运行。
