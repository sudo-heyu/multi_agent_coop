# 记忆模块（Memory Module）现状说明

> 本文档描述当前已实现的记忆子系统（截至 2026-07，schema v6）。
> 演进历史与后续路线见 `docs/memory-architecture.md`。

多 AP 协商系统的记忆模块让 LLM Agent 的每次协商**可持久、可恢复、可积累、可校验**：
运行事件全部落 SQLite，长会话上下文确定性压缩，协商结束自动沉淀为案例，
决策执行后按时间窗口采集真实效果并反哺案例质量。全链路无 LLM 参与，
纯确定性 Python，可单测。

## 设计红线

这些约束贯穿所有记忆层，后续迭代不得破坏：

1. **记忆只作参考，实时状态与确定性 Validator 永远优先。** 历史案例注入提案提示时
   明确标注"仅作参考，必须按当前最新状态重新调用工具验算"；任何决策最终都要过
   物理约束 Validator。
2. **确定性。** 摘要、特征编码、相似度、效果分类全部是纯函数，同输入必同输出；
   不引入后台模型摘要（未来若加，必须保留确定性降级路径）。
3. **幂等。** 事件按 event_id 幂等追加；评估窗口按 (run, window) 幂等登记；执行
   下发按 idempotency_key 幂等；质量修订从流水线基础分重算，重复评估不叠加。
4. **副作用保守，人在回路。** 网络结果不确定的下发标 `unknown` 并阻塞自动恢复，
   需人工核对 AP `/status` 后 `resolve-action`；效果恶化默认只产出回滚**建议与参数
   计划**，绝不自动执行——管理员显式 `--confirm` 审批后才经同一套幂等 action journal
   下发（`memory_admin.py rollback`）。
5. **评估读全量原始遥测，agent 读白名单视图。** `apply_profile` 白名单限制 LLM
   视野（只见 `throughput_mbps_user` 等协商字段）；Outcome 评估必须绕开白名单读
   iperf 吞吐/延迟/丢包，否则覆盖率不足会误判 inconclusive（实跑踩过的坑）。

## 分层总览

```text
┌────────────────────────────────────────────────────────────┐
│  L4 Outcome Evaluator   决策生效后多窗口真实效果评估          │
│      src/memory/outcome.py        ↓ 质量修订/回滚建议        │
│  L3 Episodic Memory     一次协商 = 一个案例；相似检索注入提案  │
│      src/memory/episodic.py                                 │
│  L2 Session Memory      单次长会话的确定性增量摘要 + 上下文预算│
│      src/memory/session_memory.py                           │
│  L1 Event Store + 恢复  有序幂等事件流 / 快照 / action journal│
│      src/persistence/{event_store,recovery}.py              │
└────────────────────────────────────────────────────────────┘
  规划中：L5 Semantic Memory（跨案例归纳规律）、L6 Consolidation（后台整理）
```

数据库：`logs/agent_memory.sqlite3`（WAL + foreign key + 事务）。
环境变量：`MULTIAP_EVENT_DB` 换路径，`MULTIAP_EVENT_STORE=0` 仅调试时禁用。

## L1 事件存储与恢复（src/persistence/）

### Schema v6 表

| 表 | 内容 |
|---|---|
| `schema_migrations` | 版本号 1..6，新版本就地迁移（含 ALTER 补列） |
| `agent_runs` | run 状态/模式/场景/模型/当前阶段/outcome |
| `run_events` | 严格有序事件流：event_id 主键幂等，(run_id, sequence) 唯一 |
| `state_snapshots` | 不可变状态快照：`initial`（全量原始遥测）、`final_observed`/`final_fallback` |
| `run_steps` | 可恢复步骤：状态机 + retry_budget + attempts |
| `action_journal` | 外部副作用：idempotency_key 唯一；pending→running→succeeded/failed/unknown |
| `negotiation_projections` | 最新安全协商边界投影（transcript/proposal/votes/retry/游标） |
| `session_memories` | L2 摘要与游标 |
| `episodic_memories` | L3 案例（run_id 唯一，upsert） |
| `outcome_evaluations` | L4 评估窗口：(run_id, window_label) 唯一，due_at 驱动收割 |

### 双轨日志

`SessionLogger`（src/logger.py）先写事件存储、再写 JSONL；每行 JSONL 携带
`event_id` + `sequence`，两种格式可对账。JSONL 供 Dashboard/人读，SQLite 供
恢复/记忆/检索。

### 恢复（--resume-run）

`build_checkpoint` 从事件流投影出保守 checkpoint：

- **安全边界**：`broadcast_complete` / `proposal_ready` / `vote_progress` /
  `counter_proposal_ready`，恢复跳过已完成的广播、提案、投票；
- **可恢复条件**：run 未完成 ∧ 无 running/unknown 副作用 ∧ 投影 safe_to_resume；
- **环境兼容检查**：恢复前读最新 AP 状态，可调参数/业务优先级/邻居拓扑变化即拒绝
  （QoS 数值自然波动不阻塞）；
- **幂等下发**：executor `/apply` 走 action journal——成功动作复用缓存结果不重发；
  明确失败最多 2 次尝试；网络不确定标 `unknown`，必须人工
  `memory_admin.py resolve-action` 后才能继续恢复。

## L2 Session Memory（src/memory/session_memory.py）

解决单次协商 transcript 超长问题。只压缩**注入模型的上下文**，原始事件和完整
transcript 仍完整保存在 SQLite/JSONL。

- `summarized_turns` 游标保证同一发言只摘要一次（增量、幂等）；
- 旧发言确定性分类为 `broadcast / proposal / vote / validator / message`；
  proposal 保留参数 JSON 前 500 字符，其余取正文前 500 字符；
- 摘要条目超 48 条时压缩：优先保留最近 24 条 validator/proposal 证据 + 最近 24 条；
- 最近 `--context-recent-turns`（默认 6）条发言保留原文；
- 每回合上下文硬上限 `--context-budget-chars`（默认 14000，下限 2000）字符，
  超限时保尾部（最新内容优先）；
- 每次摘要更新经 `SessionLogger.save_session_memory` 持久化，并合并进恢复投影，
  恢复后的 run 延续同一份摘要。

## L3 Episodic Memory（src/memory/episodic.py）

**物化**：`session_end` 时自动从事件流提取一个案例（`materialize_episode`）：
初始状态、拓扑签名、领域特征、最终决策、Validator 结果、executor 响应、最终观测
及吞吐/延迟/丢包前后差值、流水线质量分。按 run_id upsert，失败只记
`episodic_memory_failed` 事件不影响主流程。

**拓扑签名**：`sha256(sorted[(ap, sorted(邻居列表))])` 前 20 位十六进制。检索先按
签名**严格过滤**——不同拓扑的经验绝不跨用。

**特征与相似度**（纯函数 `feature_similarity`，逐字段归一后加权）：

| 分量 | 权重 | 字段与归一尺度 |
|---|---|---|
| 干扰 interference | 0.30 | 邻居 RSSI 链路逐条比对（25 dB 尺度；链路集合不同直接 0 分） |
| 负载 load | 0.25 | 信道占用比 + 重传率（1.0 尺度） |
| 优先级 priority | 0.15 | traffic_priority 映射 low=0 / medium=0.5 / high=1 |
| 参数 parameters | 0.20 | tx_power(23) / CWmin(1023) / CWmax(1023) / AIFSN(15) |
| STA 信号 sta | 0.10 | sta_rssi（40 dB 尺度） |

**流水线质量分**（`pipeline_quality`，满分 1.0）：
成功 outcome +0.5；Validator 通过 +0.25；executor 全部成功 +0.15（无执行 +0.05）；
有真实观测 +0.10。此为基础分，L4 评估会在其上修订。

**注入**：提案阶段（恢复到投票边界时跳过）检索最多 3 个 `quality ≥ 0.5` 的同拓扑
案例，按 (相似度, 质量) 降序注入提案提示，每条含相似度/质量/策略/结果/决策 JSON/
执行后评估结论（实际改善/恶化/无明显变化），并强制要求重新读最新状态、重新工具验算。
检索本身也记 `episodic_memory_recalled` 事件，可审计。

## L4 Outcome Evaluator（src/memory/outcome.py）

回答"决策下发后**真实世界到底变好了没有**"，并让答案反哺案例库。

**登记**：协商成功（Validator 通过、决策生效）时按窗口登记 pending 评估，
(run, window) 幂等。窗口 `--eval-windows`：mock 默认 `10,30`s，real 默认
`60,300,900`s，`off` 关闭；coordinator 兼容路径经 `MULTIAP_EVAL_WINDOWS` 透传。
**基线优先取 run 的 `initial` 快照**（全量原始遥测），传入状态仅兜底。

**收割（惰性、不阻塞、可重试）**，四条路径共用 `harvest_evaluations`
（= 尽力 `collect_due_evaluations` + `abandon_stale_evaluations`）：

1. **后台常驻 harvester**（`state_server/outcome_harvester.py`，由 `serve.sh` 拉起，
   默认每 30s）——real 模式长窗口（可达 15 分钟）不再依赖后续协商即可自动结算，
   是 L4 在真实部署下可靠的前提；
2. mock 保活循环内每 2s 结算到期窗口并实时打印 verdict；
3. 下次 `run_openclaw.py` 启动时补收上一轮到期窗口（本轮提案立即用上带反馈的案例）；
4. `memory_admin.py evaluate --server <url>` 手动结算。

状态获取失败（state server 离线/数据过期）窗口保持 pending，之后任一路径重试；
但逾期太久（`due_at` 后超过 `max(窗口×4, 1 小时)`）仍收不到有效状态的窗口会被标
`abandoned`，避免孤儿窗口永久 pending。收割与放弃**解耦**：即便收割因 state 离线
抛错，放弃逻辑仍执行。注意 abandon 是"拿不到数据"的兜底，不是"逾期即弃"——只要
state 可用，逾期窗口仍照常收割（稳态近似）。

**打分与分类**（`evaluate_deltas` + `classify`）：

- 单指标得分截断 [-1,1]：吞吐 iperf/user 相对变化；延迟相对变化取反；
  丢包用绝对百分点 /5.0 归一（基线近 0 时相对值会爆炸）；
- 每 AP 取指标均值，再按业务优先级加权聚合（high=1.0 / medium=0.6 / low=0.3）——
  协商目标就是高优先级收益最大化，低优先级"让出信道"的小幅退化不该判恶化；
- 聚合得分 ≥ +0.05 → `improved`；≤ -0.05 → `degraded`；其间 `neutral`；
  指标覆盖率 < 50% → `inconclusive`；
- 置信度 = 覆盖率 × min(1, |得分|/0.15)。

**回写**（`apply_evaluation_to_episode`）：最终 verdict 取最后一个有定论的窗口
（最接近稳态）。质量修订从流水线基础分重算（幂等）：

- `improved` → base + 0.15 × 置信度（封顶 1.0）；
- `degraded` → 封顶 **0.2**，低于注入阈值 0.5，恶化方案退出提案参考池；
- `degraded` 且置信度 ≥ 0.5 → `needs_rollback=true` 并生成 `rollback_plan`
  （只含决策实际改过的字段、恢复到协商前值）。

**回滚执行通道**（`src/memory/rollback.py`，`memory_admin.py rollback <run_id>`）：
默认 dry-run 只回显计划不发请求；管理员核对后加 `--confirm --ap-endpoints ...` 才
下发。下发走 action journal 幂等（成功不重发、`unknown` 阻塞待人工核对），且
`rollback_plan` 的值本就是 executor `/apply` 的 wire 格式（CW 为指数、tx_power 为
实际 dBm），直接下发**不做二次编码**（与决策下发的 `encode_params_edca` 路径不同）。

**实测样例**（mock EDCA，run `0badeaed`）：协商 94s 成功 → 窗口 t+10s/t+30s 均判
"实际改善"（置信度 0.95/0.91）——高优先级直播 AP 延迟 312→185ms、吞吐 +24%，
低优先级 AP 小幅让出——案例质量 0.8 → 0.937。

## 一次协商的记忆生命周期

```text
run 开始     session_start → agent_runs + initial 快照（全量遥测）
   │
广播/提案    transcript 超预算 → L2 增量摘要（session_memories）
   │         提案前 → L3 检索同拓扑案例注入（≤3 个，quality≥0.5）
   │         每个安全边界 → negotiation_projections checkpoint
   │
投票/决策    Validator 验算 → 通过则 executor 下发（action journal 幂等）
   │         成功 → L4 登记评估窗口（基线=initial 快照）
   │
run 结束     session_end → L3 物化案例（流水线质量分）
   │
窗口到期     后台 harvester/启动补收/手动 → 收割 verdict、逾期则放弃
   │         → 修订案例质量（±）→ degraded 生成回滚建议
   │
回滚(可选)   memory_admin rollback：dry-run 预演 → --confirm 幂等下发
   │
下一次 run   提案注入带真实反馈的案例（恶化案例已被踢出参考池）  ←──循环
```

## 运维与查询

```bash
.venv/bin/python memory_admin.py incomplete                 # 未完成 run
.venv/bin/python memory_admin.py show <run_id>              # checkpoint+事件+快照+动作
.venv/bin/python memory_admin.py resolve-action <id> --status succeeded --note "..."
.venv/bin/python memory_admin.py episodes [--scene edca] [--limit 20]
.venv/bin/python memory_admin.py similar <run_id> [--limit 5] [--min-quality 0.5]
.venv/bin/python memory_admin.py evaluate --server http://localhost:5001 [--run <id>]
.venv/bin/python memory_admin.py evaluations <run_id>       # 窗口结论+回滚建议
.venv/bin/python memory_admin.py rollback <run_id>          # 预演回滚计划（dry-run）
.venv/bin/python memory_admin.py rollback <run_id> --ap-endpoints ap1=host:port,... --confirm
.venv/bin/python run_openclaw.py --resume-run <run_id>      # 从安全边界恢复
```

后台收割器随常驻服务启动：`bash openclaw/serve.sh start` 会拉起 `outcome_harvester`，
`serve.sh status` 显示其状态。

## 配置汇总

| 配置 | 默认 | 作用 |
|---|---|---|
| `MULTIAP_EVENT_DB` | `logs/agent_memory.sqlite3` | 数据库路径 |
| `MULTIAP_EVENT_STORE` | `1` | 置 0 禁用事件存储（仅调试） |
| `--context-budget-chars` | 14000 | 每回合注入上下文字符预算（≥2000） |
| `--context-recent-turns` | 6 | 保留原文的最近发言数（≥2） |
| `--eval-windows` / `MULTIAP_EVAL_WINDOWS` | mock=`10,30` real=`60,300,900` | 评估窗口秒数，`off` 关闭 |
| `MULTIAP_HARVEST_INTERVAL` | 30 | 后台 harvester 两次收割间隔秒数 |

## 测试

`tests/` 内 68 个确定性用例覆盖记忆全链路：事件存储迁移（v1→v6 就地升级）、幂等
追加/下发、恢复边界与阻塞、Session 摘要游标与预算、案例物化/拓扑隔离/相似排序、
评估调度幂等、分类阈值、优先级加权、按期收割、质量修订幂等；L4 可靠性：逾期放弃
（abandon grace）、收割/放弃解耦、回滚 dry-run、幂等下发、wire 格式不二次编码。

## 演进路线

已完成 L1–L4，**含 L4 可靠性闭环**（后台 harvester、逾期放弃、人工审批回滚执行）。
下一步（见 `docs/memory-architecture.md`）：

1. **L5 Semantic Memory**：从多个带 Outcome 反馈的案例归纳规律（带证据引用与
   置信度），前提正是 L4——否则归纳的是"跑完了的方案"而非"有效的方案"；
2. **L6 Consolidation**：带锁、门控、冲突检测和过期管理的后台整理；
3. 评估因果对照与参数校准、记忆可观测性（Dashboard 集成）。
