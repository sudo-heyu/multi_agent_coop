# 记忆模块使用与设计说明

本文说明 Multi-AP 协商系统如何记住一次对话、如何积累历史经验，以及管理员如何
查看、恢复和清理这些记忆。

## 先说结论

系统同时具备短期记忆和长期记忆：

| 记忆 | 通俗理解 | 解决的问题 | 保存位置 |
|---|---|---|---|
| 会话记忆 | 当前这次会议的会议纪要 | 对话太长时，Agent 仍能记住前文 | `session_memories` |
| 案例记忆 | 过去每次协商的案例档案 | 下次遇到相似情况，可以参考以前怎么处理 | `episodic_memories` |
| 语义记忆 | 从多个案例中总结出的经验规律 | 不只记住单个案例，还能判断某类策略通常是否有效 | `semantic_rules` |

此外，系统还会保存完整事件、状态快照和执行记录，用于故障恢复和审计。所有持久化
数据默认写入：

```text
logs/agent_memory.sqlite3
```

一句话概括整个闭环：

```text
记下过程 → 压缩长对话 → 保存成功案例 → 观察实际效果
    → 调整案例可信度 → 总结规律 → 下次协商时参考
```

## 一次协商中，记忆是怎样工作的

假设系统正在处理一次信道拥塞：

1. **协商开始**：保存当前 AP 状态，作为后续比较的基线。
2. **Agent 讨论**：完整发言写入事件日志。对话过长时，早期内容被整理成简短摘要，
   最近几轮仍保留原文。
3. **准备提案**：系统查找“拓扑相同、负载和干扰相似”的历史案例，并读取已有经验
   规律，最多各取 3 条放进提案上下文。
4. **验证与执行**：提案仍必须读取最新状态并经过 Validator；历史经验不能直接替代
   实时数据或绕过校验。
5. **协商结束**：本次环境、决策、验证结果和执行结果被保存为一个新案例。
6. **延迟观察**：在多个时间窗口重新读取吞吐、时延和丢包，判断实际效果是改善、
   恶化、基本不变，还是数据不足。
7. **反馈学习**：改善案例提高可信度；恶化案例降权并退出推荐池；多个案例积累后，
   系统归纳出更稳定的经验规律。

流程图：

```text
开始协商
   │
   ├─ 保存初始状态和完整事件
   │
   ├─ 对话过长？── 是 ──> 摘要早期发言，保留最近原文
   │
   ├─ 提案前检索历史案例和经验规律
   │                  │
   │                  └─ 只作参考，仍需读取实时状态并重新验算
   │
   ├─ Validator 通过 ──> 幂等下发到 AP
   │
   ├─ 结束时保存一个完整案例
   │
   └─ 多窗口观察效果
          ├─ 改善：提高案例质量
          ├─ 恶化：降低质量，生成回滚建议
          └─ 多个案例：归纳或更新经验规律
```

## 三种真正给 Agent 使用的记忆

### 1. 会话记忆：记住当前协商说过什么

会话记忆只服务于**当前这一次协商**。

当对话未超过上下文预算时，Agent 看到原始对话。超过预算后，系统把较早发言转换为
结构化摘要，例如：

```text
- turn=2 speaker=ap1 kind=broadcast: AP1 当前重传率较高……
- turn=5 speaker=ap2 kind=proposal: 参数JSON={...}
- turn=7 speaker=VALIDATOR kind=validator: CWmin 不满足约束……
```

处理原则：

- 默认保留最近 6 条发言原文；
- 每个 Agent 回合的上下文默认最多 14,000 字符；
- 提案参数和 Validator 证据优先保留；
- 摘要使用确定性 Python 逻辑，不调用额外模型；
- 只压缩给模型看的上下文，SQLite 和 JSONL 中的完整原文不会丢失；
- 摘要和处理游标会持久化，中断恢复后不会从头重复摘要。

对应实现：`src/memory/session_memory.py`。

### 2. 案例记忆：记住以前发生过什么

每次协商结束后，系统自动生成一份案例档案，内容包括：

- 初始 AP 状态和邻居拓扑；
- 当时选择的策略和最终参数；
- Validator 是否通过；
- executor 是否成功下发；
- 执行后的状态和效果评估；
- 案例质量分。

案例质量分在 `[0, 1]` 范围内，分为“流水线基础分”和“实际效果修正”两步计算。

第一步根据本次协商流程是否完整可靠计算基础分：

| 条件 | 加分 |
|---|---:|
| 协商结果为 `success` | `+0.50` |
| Validator 验证通过 | `+0.25` |
| 存在 executor 记录且全部下发成功 | `+0.15` |
| 没有 executor 记录 | `+0.05` |
| 存在执行后的观测状态 | `+0.10` |

```text
基础质量分 = 协商结果分 + Validator 分 + 执行分 + 观测分
基础质量分最高为 1.0
```

如果存在 executor 记录但有任何一次下发失败，执行项记 `0` 分，不再按“没有
executor”计 `0.05` 分。例如，mock 模式下协商成功、验证通过、没有 executor、
存在观测状态时，基础质量分为 `0.50 + 0.25 + 0.05 + 0.10 = 0.90`。

第二步在效果评估窗口产生最终结论后修正质量分。设基础质量分为 `Q`，效果评估的
最终置信度为 `C`：

```text
improved：最终质量分 = min(1.0, Q + 0.15 × C)
degraded：最终质量分 = min(Q, 0.20)
neutral / inconclusive：最终质量分 = Q
```

例如，基础质量分为 `0.80`、最终结论为 `improved`、最终置信度为 `0.60` 时，
最终质量分为 `0.80 + 0.15 × 0.60 = 0.89`。如果最终结论为 `degraded`，即使
协商、验证和下发流程全部成功，最终质量分也最高只有 `0.20`，低于默认案例召回
阈值 `0.50`，因此不会作为优质历史案例注入后续提案。

下次协商检索案例时，先要求**拓扑完全一致**，再比较以下内容：

| 相似因素 | 权重 | 直观含义 |
|---|---:|---|
| 干扰关系 | 30% | 邻居 RSSI 是否接近 |
| 当前负载 | 25% | 信道占用和重传率是否接近 |
| 业务优先级 | 15% | high / medium / low 是否相似 |
| 当前参数 | 20% | 功率、CWmin、CWmax、AIFSN 是否接近 |
| STA 信号 | 10% | STA RSSI 是否接近 |

最多向提案上下文注入 3 个质量分不低于 0.5 的案例。不同拓扑的案例不会混用，
已归档和实际效果恶化的案例不会作为优质经验推荐。

对应实现：`src/memory/episodic.py`。

### 3. 语义记忆：总结“通常什么方法有效”

单个案例可能是偶然结果。语义记忆会把多个有实际效果反馈的案例按“拓扑、场景、
策略”分组，归纳出规律，例如：

```text
在拓扑 A 的 EDCA 场景中，co_edca 在 3 个已评估案例中通常带来改善；
典型参数为 ap2 CWmin=3；一致性 100%，置信度 1.0。
```

形成规律需要满足：

- 至少有 2 个结论明确的案例；
- 只使用已经完成效果评估的案例；
- 规律保留证据 run_id，可追溯到原始案例；
- 置信度不低于 0.5 才会注入提案；
- 证据互相矛盾时标记为 `conflicted`，停止注入。

规律的**一致性**表示支持主导结论的案例比例。设同一分组内有 `N` 个结论明确的
案例，其中出现次数最多的结论（`improved`、`neutral` 或 `degraded`）有 `D` 个，则：

```text
一致性 consistency = D / N
```

规律的**置信度**同时考虑一致性和证据数量。当前实现把 3 个案例视为证据数量充分，
不足 3 个时按比例折减，超过 3 个后数量系数不再增加：

```text
置信度 confidence = consistency × min(1, N / 3)
```

例如，2 个案例均为 `improved` 时，一致性为 `2/2 = 1.0`，置信度为
`1.0 × 2/3 = 0.6667`；3 个案例中 2 个为 `improved`、1 个为 `neutral` 时，
一致性为 `2/3 = 0.6667`，置信度也是 `0.6667 × 1 = 0.6667`。计算结果保留
4 位小数。案例自身效果评估里的 `final_confidence` 当前只作为证据元数据保存，
不参与上述规律置信度计算。

对应实现：`src/memory/semantic.py`。

## 系统怎样判断一个方案是否真的有效

协商成功不等于网络真的改善。因此，决策下发后还会登记多个观察窗口：

| 模式 | 默认观察时间 |
|---|---|
| mock | 10 秒、30 秒 |
| real | 60 秒、300 秒、900 秒 |

窗口到期后，系统比较协商前后的：

- 吞吐量：越高越好；
- 时延：越低越好；
- 丢包率：越低越好。

高优先级业务的权重高于低优先级业务。最终结论有四种：

| 结论 | 含义 |
|---|---|
| `improved` | 综合得分明显改善 |
| `degraded` | 综合得分明显恶化 |
| `neutral` | 变化不明显 |
| `inconclusive` | 有效指标不足，不能判断 |

系统不会因为一个瞬时采样就轻易认定有效。多个窗口持续同向时，结论置信度较高；
窗口之间方向摇摆时，置信度会降低。

反馈会反向修改案例质量：

- `improved`：按置信度提高质量分；
- `degraded`：质量分最高只能为 0.2，因此不会再次作为优质案例注入；
- 高置信度恶化：生成恢复到协商前参数的回滚建议；
- 回滚只生成建议，不会自动执行，必须由管理员显式确认。

对应实现：`src/memory/outcome.py` 和 `src/memory/rollback.py`。

## 故障恢复和安全边界

记忆模块也承担运行恢复职责。系统持续记录：

- 有序事件和状态快照；
- 已完成的广播、提案和投票；
- 当前安全 checkpoint；
- executor 下发动作及其结果。

异常退出后，可执行：

```bash
.venv/bin/python run_openclaw.py --resume-run <run_id>
```

恢复只会从已确认的安全边界继续，并跳过已完成步骤。以下情况会拒绝自动恢复：

- AP 参数、业务优先级或邻居拓扑已经改变；
- 外部下发动作仍处于 `running` 或结果不确定的 `unknown`；
- 当前事件投影没有安全恢复点。

executor 调用使用幂等 key。已经成功的动作不会重复发送；明确失败最多尝试两次；
网络超时导致结果不确定时，必须先人工检查 AP 的 `/status`，然后标记真实结果：

```bash
.venv/bin/python memory_admin.py resolve-action <action_id> \
  --status succeeded --note "已核对 AP 状态"
```

对应实现：`src/persistence/event_store.py` 和 `src/persistence/recovery.py`。

## 记忆如何防止越积越乱

后台整理模块会定期维护案例和规律：

1. 每种拓扑默认最多保留 50 个高质量案例；
2. 超过 90 天且质量低于 0.3 的案例被归档；
3. 基于仍然有效的案例重新归纳语义规律；
4. 证据一致性低于 0.6 的规律标记为冲突。

归档采用软删除：数据仍保留以便审计，但不再参与检索和归纳。整理过程带维护锁，
不会与另一个整理任务同时修改数据。

对应实现：`src/memory/consolidation.py`。

## 数据实际保存在哪里

默认数据库是 `logs/agent_memory.sqlite3`，采用 SQLite WAL、外键和事务。主要数据表：

| 表 | 保存内容 |
|---|---|
| `agent_runs` | 每次运行的状态、场景、阶段和最终结果 |
| `run_events` | 严格有序的完整事件流 |
| `state_snapshots` | 初始和最终 AP 状态快照 |
| `run_steps` | 可恢复步骤及重试状态 |
| `action_journal` | executor 等外部副作用及幂等状态 |
| `negotiation_projections` | 最近的安全协商 checkpoint |
| `session_memories` | 当前会话摘要和摘要游标 |
| `episodic_memories` | 历史协商案例 |
| `outcome_evaluations` | 各观察窗口的效果结论 |
| `semantic_rules` | 跨案例归纳的经验规律 |
| `maintenance_locks` | 后台整理任务的互斥锁 |

`SessionLogger` 同时写 SQLite 和 JSONL：SQLite 用于恢复、检索和统计，JSONL 用于
Dashboard 展示和人工阅读。两边都携带 `event_id` 和 `sequence`，可以对账。

## 常用查看和维护命令

### 查看整体健康状况

```bash
.venv/bin/python memory_admin.py health
```

重点关注：

- `evaluations.pending` 持续增加：后台收割器可能没有运行；
- `evaluations.abandoned` 增加：state server 可能长期离线或数据过期；
- `episodes.degraded_ratio` 较高：近期决策质量可能下降；
- `rules.conflicted` 增加：环境可能变化，历史规律不再稳定。

Dashboard 页面：`http://localhost:5050/memory`。

### 查询运行、案例和规律

```bash
.venv/bin/python memory_admin.py incomplete
.venv/bin/python memory_admin.py show <run_id>
.venv/bin/python memory_admin.py episodes --limit 20
.venv/bin/python memory_admin.py similar <run_id> --min-quality 0.5
.venv/bin/python memory_admin.py evaluations <run_id>
.venv/bin/python memory_admin.py rules --min-confidence 0.5
```

### 手动评估和整理

```bash
.venv/bin/python memory_admin.py evaluate --server http://localhost:5001
.venv/bin/python memory_admin.py rules --induce
.venv/bin/python memory_admin.py consolidate
.venv/bin/python memory_admin.py calibrate
```

后台评估器由常驻服务启动：

```bash
bash openclaw/serve.sh start
bash openclaw/serve.sh status
```

### 审批回滚

先预览，不发送请求：

```bash
.venv/bin/python memory_admin.py rollback <run_id>
```

核对计划后显式执行：

```bash
.venv/bin/python memory_admin.py rollback <run_id> \
  --ap-endpoints ap1=host:port,ap2=host:port,ap3=host:port --confirm
```

## 常用配置

| 配置 | 默认值 | 作用 |
|---|---:|---|
| `MULTIAP_EVENT_DB` | `logs/agent_memory.sqlite3` | 修改数据库路径 |
| `MULTIAP_EVENT_STORE` | `1` | 设为 `0` 可关闭事件存储，仅建议调试使用 |
| `--context-budget-chars` | `14000` | 单回合上下文字符上限，不能低于 2000 |
| `--context-recent-turns` | `6` | 最近保留原文的发言数，不能低于 2 |
| `--eval-windows` | mock=`10,30`；real=`60,300,900` | 效果观察窗口；`off` 表示关闭 |
| `MULTIAP_HARVEST_INTERVAL` | `30` | 后台评估器轮询间隔，单位为秒 |
| `MULTIAP_IMPROVE_THRESHOLD` | `0.05` | 判定改善的得分阈值 |
| `MULTIAP_DEGRADE_THRESHOLD` | `-0.05` | 判定恶化的得分阈值 |
| `MULTIAP_MIN_COVERAGE` | `0.5` | 最低有效指标覆盖率 |
| `MULTIAP_ROLLBACK_CONFIDENCE` | `0.5` | 生成回滚建议的最低置信度 |

## 不可突破的安全原则

无论记忆积累了多少案例，都必须遵守以下规则：

1. **实时状态优先**：历史案例只供参考，不能替代最新 AP 状态。
2. **Validator 优先**：历史上成功的参数，本次也必须重新验算。
3. **默认不自动回滚**：恶化只生成建议，管理员确认后才能下发。
4. **不确定副作用不重试**：网络结果未知时先人工核对，避免重复配置 AP。
5. **完整数据可审计**：摘要不会删除原始事件，归档不会物理删除案例。
6. **确定性和幂等**：摘要、检索、评估和整理同输入应得到同结果；重复执行不应产生
   重复副作用。

## 代码入口与测试

| 功能 | 代码位置 |
|---|---|
| 会话摘要 | `src/memory/session_memory.py` |
| 案例生成与检索 | `src/memory/episodic.py` |
| 效果评估 | `src/memory/outcome.py` |
| 回滚建议与执行 | `src/memory/rollback.py` |
| 规律归纳 | `src/memory/semantic.py` |
| 后台整理 | `src/memory/consolidation.py` |
| 健康度统计 | `src/memory/observability.py` |
| SQLite 存储 | `src/persistence/event_store.py` |
| 中断恢复 | `src/persistence/recovery.py` |
| 管理命令 | `memory_admin.py` |

测试位于 `tests/`，覆盖数据库迁移、事件幂等、会话摘要、案例隔离与排序、效果评估、
回滚、规律归纳、后台整理、恢复边界和健康度统计。

当前仍有两个明确限制：

- 案例按拓扑签名严格隔离，新拓扑无法直接复用其他拓扑经验；
- 会话摘要目前是确定性规则摘要，不是模型生成的自然语言总结。

后续研究方向见 `docs/memory-architecture.md`。
