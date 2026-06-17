# Multi-AP 协商系统 —— 纯 OpenClaw 架构迁移计划

> **本文件是临时迁移计划。当「验收标准」一章的所有项全部勾选完成后，删除本文件（`rm OPENCLAW_MIGRATION_PLAN.md`）并在提交信息中注明迁移完成。**

---

## 0. 背景

本项目是一个 **多 AP（Wi-Fi 接入点）无线参数自主协商系统**：DGX Spark 上运行本地大模型（Ollama `qwen3:14b` / PPIO 云端 `qwen:80b`）+ Flask 状态服务器 + 编排器，三台香蕉派 AP 实测上报数据，三个 LLM Agent 以自然语言协商，协调发射功率（Co-SR）与 MAC 退避参数（Co-EDCA），由确定性 Validator 做物理约束验收，最后把决策下发到香蕉派执行。

**当前实现**：编排逻辑是自研 Python（`src/orchestrator.py`），Agent 通过 `src/agent.py` 直连 Ollama/PPIO，工具是进程内 Python 函数（`src/tools/`）。项目早期借鉴了 OpenClaw 的 agent workspace 文件约定（`IDENTITY/SOUL/AGENTS/TOOLS.md`），但**运行时并未使用 OpenClaw**。

**本次目标**：把**托管层**与**编排层**都改为真正的 OpenClaw 运行时，使项目主体成为「纯 OpenClaw 架构」，同时**保持现有功能与效果不变**，并能在两种 mock 模式下复现现在的结果。

### 0.1 为什么做这件事（决策已定）

已与维护者确认：明确要求「托管和编排层都使用 OpenClaw」。已知的权衡（编排交给 LLM coordinator 会把原本由 Python 代码保证的确定性控制流，降级为模型尽力遵守的协议）维护者已知悉并接受。本计划在此前提下，用「确定性逻辑封装为工具 + LLM 仅负责串联」的方式，尽可能逼近原有效果。

---

## 1. 必须用 OpenClaw 达成的东西（硬性要求）

1. **托管层**：`coordinator / ap1 / ap2 / ap3` 必须是真正的 OpenClaw agent（各自独立 workspace、session、身份），由 OpenClaw Gateway/embedded runtime 运行，模型经 OpenClaw 的 provider 机制调用（ollama / PPIO）。
2. **编排层**：四阶段协商流程必须由 **coordinator 这个 OpenClaw agent（LLM）** 驱动，**不得**再由 `src/orchestrator.py` 这样的外部 Python 编排器主导控制流。coordinator 通过工具（含驱动子 agent 的 `ask_ap`）推进协议。
3. **工具层**：现有确定性计算/验算/状态/下发逻辑以 **OpenClaw MCP 工具**形式暴露给 agent 调用（允许保留 Python 实现，但必须通过 OpenClaw 的工具调用机制被使用，而非 prompt 注入或进程内直调）。
4. **入口**：协商通过 `openclaw agent --agent coordinator ...` 触发；不再走 `python run.py` 里的 `NegotiationOrchestrator.run()` 作为编排主体（`run.py` 可降级为「准备数据 + 拉起 coordinator」的薄启动器）。
5. **隔离**：全部配置位于独立 profile（`~/.openclaw-multiap/`），**不得**改动用户默认 profile（C3-PO）。

---

## 2. 必须复现的效果（验收口径）

> **关于「复现」的口径**：LLM 输出非确定，不要求逐字一致。「复现」指：**相同的协议路径选择、相同的约束保证、Validator 通过的合法决策、相同的可观测四阶段流程、相同的日志/可视化产物**。即「结构与约束等价」，而非「文本逐字相同」。

### 2.1 两种 mock 模式（核心交付）
- **Mock A —— 预设场景**：等价于现 `python run.py --mock --scene {sr,edca,joint}`。三套硬编码初始状态（见 `run.py` 的 `MOCK_SCENE_SR/EDCA/JOINT`）。
- **Mock B —— 曲线喂数器**：等价于现 `state_server/mock_feeder.py`，向状态服务器持续喂入随时间变化的遥测，协商决策注入后曲线体现协商后改善。

三个场景的预期路径：
| 场景 | 触发 | 预期策略 |
|---|---|---|
| `sr` | 邻居 RSSI 强（>-70dBm 区间）、各 AP 业务优先级相同 | `co_sr`（降功率） |
| `edca` | 各 AP 业务优先级分化（high/low），邻居弱 | `co_edca`（差异化 CWmin/CWmax/AIFSN） |
| `joint` | 高功率 + 优先级分化同时成立 | `joint`（功率 + EDCA 联动） |

### 2.2 四阶段协商流程（必须可观测）
1. **广播**：ap1→ap2→ap3 依次播报自身实测状态；只播报己方数据 + 本机扫描的邻居 RSSI，不引用他人业务指标。
2. **提案**：首轮固定由 ap1 发起、自主选路；提案前必须调用 `get_latest_ap_states`，Co-SR/联合路径必须先 `analyze_sr_interference → select_sr_concurrent_groups` 选并发组；提交前自检（`evaluate_sr_candidate` / `validate_edca_proposal`）。
3. **投票**：非提案 AP 逐一表态 `同意/弃权/反对`；反对者当场给出反提案并接管为新提案方。
4. **决策 + 验收**：全票通过后输出最终 JSON（顶层键 ap1/ap2/ap3）；**确定性 Validator** 做参数范围 + 整数功率 + （真实观测时）生效校验；通过则下发执行。

### 2.3 必须保留的确定性约束（Validator）
- Co-SR：`tx_power_dbm ∈ [1,23]`；功率调整量相对协商前必须为**整数 dB**；CCA / SINR / STA-RSSI 约束（由计算工具保证）。
- Co-EDCA：`CWmin∈[3,1023]`、`CWmax∈[7,1023]`、`AIFSN∈[1,15]`、`CWmax>CWmin`；优先级单调性 `high.CWmin ≤ medium ≤ low`（AIFSN 同理）。

### 2.4 业务画像 + 字段白名单（必须一致）
进入协商前统一 `apply_profile`：业务优先级**硬编码**（ap1=抖音视频/high，ap2=下载游戏/low，ap3=下载游戏/low），只保留白名单字段，上报的 cwmin/cwmax 指数 n 统一解码为实际 CW 值。`noise_floor_dbm` 为内部字段（SINR 计算用），不展示给 agent。

### 2.5 协商控制约束（必须保留语义）
- 重投上限 `MAX_VOTE_ROUNDS=3`、验证重试 `MAX_VALIDATION_RETRIES=3`、单轮最大发言 `MAX_TURNS=30`、工具调用上限。**必须保证终止**（不收敛时干净退出，不得死循环）。
- 反对者接管、弃权等同同意等表决语义。

### 2.6 配套产物（必须保留）
- **结构化日志**：每次运行一个 JSONL，含 `session_start / phase_start / agent_speak / tool_call / vote / final_decision / validation_result / session_end` 等事件。
- **Dashboard**：Flask + SSE 实时流式展示协商对话。
- **学术曲线**：Matplotlib 业务指标曲线窗口。
- **下发执行**：协商成功后并发 POST 决策到各香蕉派 `/apply`（EDCA 发送前转指数 n）。

---

## 3. 目标架构

```
┌──────────────────────────────────────────────────────────────┐
│ OpenClaw（profile: multiap, ~/.openclaw-multiap，隔离）        │
│                                                              │
│  Agents（托管层）：coordinator / ap1 / ap2 / ap3              │
│    - 各自 workspace：IDENTITY/SOUL/AGENTS/TOOLS.md            │
│    - 模型：ollama/qwen3:14b（默认）或 PPIO openai-compatible   │
│                                                              │
│  coordinator（编排层，LLM）：AGENTS.md = 四阶段协议 standing   │
│    orders；用工具推进协议、驱动 AP、计票、验收、下发           │
│                                                              │
│  MCP 工具服务 multiap-tools（openclaw/mcp/multiap_mcp.py）：   │
│    get_latest_ap_states / analyze_sr_interference /          │
│    compute_sr_feasible_ranges / select_sr_concurrent_groups /│
│    evaluate_sr_candidate / rank_sr_candidates /              │
│    validate_edca_proposal / validate_decision /             │
│    push_decision / ask_ap(驱动子 agent) / log_event          │
└──────────────────────────────────────────────────────────────┘
        │ MCP stdio                      │ openclaw agent --local
        ▼                                ▼
  src/tools(sr,edca) · validator ·  state_server（Flask，不变）
  profile · state_client · executor  ← mock_feeder / 预设场景 POST
```

**保留为 Python（合法，作为 OpenClaw 工具/外部基础设施）**：`src/tools/sr.py`、`src/tools/edca.py`、`src/validator.py`、`src/profile.py`、`src/state_client.py`、`state_server/*`、`dashboard/*`、`state_server/academic_plot.py`。这些**逻辑零改写**，保证「效果不变」。

**新增**：`openclaw/`（setup、mcp、4 个 agent workspace）、薄启动器 `run_openclaw.py`。

**退役 / 降级**：`src/orchestrator.py`（编排主体）、`src/agent.py`（直连 LLM）不再作为运行主路径（保留作对照基线，直到迁移验收通过）。

---

## 4. 分阶段执行计划

> 进度同时维护在 OpenClaw 任务列表中；本文档为权威说明。

### Stage 0 —— 基座与隔离 ✅ 已完成
- [x] 独立 profile `multiap`，配置 ollama provider（qwen3:14b），不动默认 profile。
- [x] 创建 agent，验证 `openclaw agent --local --agent ap1` 经 ollama 正常出话、读取 workspace。

### Stage 1 —— MCP 工具服务 ✅ 已完成
- [x] `openclaw/mcp/multiap_mcp.py`：复用 `src/tools/sr.py、edca.py`、`validator.py`、`profile.py`、`state_client.py`，无状态、经状态服务器取真值。
- [x] `openclaw mcp set` 注册到 multiap profile。
- [x] 验证 agent 真实调用 `get_latest_ap_states`，返回值与 JOINT 场景一致（ap1 tx=20.0dBm，邻居 ap2=-68.4dBm）；计算/验算工具结果与 Python 实现逐项比对一致。

### Stage 2 —— 移植 AP 协商提示词
- [ ] 将 `agents/ap{1,2,3}/{IDENTITY,SOUL,AGENTS,TOOLS}.md` 适配进 `openclaw/workspaces/ap*/`，工具名对齐 MCP 工具名。
- [ ] 限制 AP agent 的工具集（AP 不应能调用 `ask_ap`），通过 per-agent tool allow/deny。
- [ ] 验收：单个 AP 能完整广播；ap1 能在 joint 场景下完成一次合法提案（含工具链 + JSON）。

### Stage 3 —— coordinator 协议编排（核心难点）
- [ ] 写 `openclaw/workspaces/coordinator/AGENTS.md`：四阶段协议 + 表决/重试/接管/终止规则的 standing orders。
- [ ] 完善 `ask_ap`（驱动子 agent）；新增确定性辅助工具：`infer_strategy`、`extract_proposal`、`tally_votes`、`pick_proposer`、以及**带状态计数器的终止工具**（把 `MAX_*` 上限做成工具返回 STOP，保证终止确定性）。
- [ ] `validate_decision` / `push_decision` 接入 coordinator 收尾。
- [ ] 验收：joint 场景跑通一次端到端协商，输出 Validator 通过的合法决策。

### Stage 4 —— 两种 mock 复现 + 配套产物
- [ ] `run_openclaw.py` 薄启动器：准备场景（Mock A 直接 POST 预设场景到状态服务器；Mock B 启动 `mock_feeder`）→ 拉起状态服务器/Dashboard/曲线 → `openclaw agent --agent coordinator` 触发。
- [ ] `log_event` MCP 工具或解析 `openclaw agent --json`，复现 JSONL 日志事件与 Dashboard 流式展示。
- [ ] 协商成功后把决策注入喂数器（Mock B 曲线体现改善）+ 下发执行。
- [ ] 验收：`sr/edca/joint` 三场景在两种 mock 下均产出符合 2.1 预期路径、Validator 通过的决策；与现 Python 实现对照「结构与约束等价」。

### Stage 5 —— PPIO 模型 / 鲁棒性 / 文档 / 测试
- [ ] 增加 PPIO（`qwen:80b`）为 openai-compatible provider，可切换模型。
- [ ] 协议遵从率加固（coordinator 重读进度、工具返回强制下一步提示、终止计数器）。
- [ ] 更新 `README.md` / `docs/` 描述纯 OpenClaw 架构；调整或重写 `tests/`（现有测试假设旧 Python 路径）。
- [ ] 决定 `src/orchestrator.py`、`src/agent.py` 的去留（迁移验收通过后退役或标注为对照基线）。

---

## 5. 验收标准（全部勾选后删除本文档）

- [ ] **托管**：`coordinator/ap1/ap2/ap3` 均为 OpenClaw agent，经 `openclaw agent` 运行（ollama 与 PPIO 均可）。
- [ ] **编排**：四阶段协议由 coordinator（LLM）经工具驱动，无外部 Python 编排器主导控制流。
- [ ] **工具**：所有计算/验算/状态/下发经 MCP 工具被 agent 调用，结果与现 Python 实现等价。
- [ ] **Mock A**：`sr/edca/joint` 三场景各跑通一次，策略路径符合 2.1，产出 Validator 通过的合法决策。
- [ ] **Mock B**：曲线喂数器驱动，协商完成后曲线体现协商后改善。
- [ ] **确定性约束**：2.3 的全部 Validator 约束在最终决策上成立；2.5 的终止保证成立（不死循环）。
- [ ] **业务画像/白名单**：2.4 与现实现一致。
- [ ] **配套产物**：JSONL 日志、Dashboard 流式、学术曲线、下发执行 均可用。
- [ ] **隔离**：用户默认 profile（C3-PO）未被改动。
- [ ] **文档/测试**：README/docs 更新为纯 OpenClaw 架构，测试通过或已相应调整。

> 以上全部完成后：删除本文件，提交 `chore: 纯 OpenClaw 迁移完成，移除迁移计划文档`。

---

## 6. 风险与缓解

| 风险 | 缓解 |
|---|---|
| coordinator（LLM）不忠实执行循环/计票/终止 | 硬逻辑全部封装为确定性 MCP 工具；终止做成带计数器的工具返回 STOP；coordinator 仅串联 |
| 长协商上下文漂移 / 压缩丢失协议进度 | 协议状态写入外部（状态服务器 + 会话状态文件）；coordinator 每步重读进度工具 |
| 子 agent 调用嵌套（ask_ap 内再起 openclaw agent） | 各 `openclaw agent --local` 为独立进程，工具无状态经状态服务器取真值，天然隔离 |
| 与现实现「文本不一致」被误判为不达标 | 复现口径＝结构与约束等价（见第 2 章），非逐字一致 |
| OpenClaw 版本演进导致配置面变动 | 配置集中在 `openclaw/setup.sh`，可一键重建 |

---

## 7. 关键路径与命令速查

```bash
# 一次性配置（隔离 profile，生成 token，注册 MCP）
bash openclaw/setup.sh

# 状态服务器（mock 允许）
python3 state_server/server.py --allow-mock &

# 单 agent 冒烟
OLLAMA_API_KEY=ollama-local openclaw --profile multiap agent --local --agent ap1 --thinking off -m 'hi' --json

# （Stage 4 后）一键跑某场景
python3 run_openclaw.py --mock --scene joint
```

相关源码：编排参照基线 `src/orchestrator.py`；工具实现 `src/tools/{sr,edca}.py`；验收器 `src/validator.py`；画像 `src/profile.py`；两种 mock `run.py`(MOCK_SCENE_*) 与 `state_server/mock_feeder.py`。
