# Agent Memory 架构与实施状态

## 当前阶段

第一阶段已建立本地 SQLite Event Store。它与 JSONL 双写，不改变当前 OpenClaw 协商行为，为后续任务恢复、案例记忆、效果评估和检索提供持久化基础。

默认数据库：`logs/agent_memory.sqlite3`。

可通过环境变量覆盖：

```bash
MULTIAP_EVENT_DB=/path/to/agent.sqlite3 .venv/bin/python run_openclaw.py
MULTIAP_EVENT_STORE=0 .venv/bin/python run_openclaw.py  # 仅调试时禁用
```

## Schema v6

| 表 | 用途 |
|---|---|
| `schema_migrations` | 数据库版本 |
| `agent_runs` | run 状态、模式、场景、当前阶段和 outcome |
| `run_events` | run 内严格有序、event ID 幂等的事件流 |
| `state_snapshots` | 初始、最终观测等不可变状态快照 |
| `run_steps` | 可恢复步骤、尝试次数和 retry budget |
| `action_journal` | 外部副作用 intent、幂等 key、请求、结果与不确定状态 |
| `negotiation_projections` | 最新安全边界及 transcript/proposal/vote/retry 投影 |
| `session_memories` | 增量摘要、摘要游标、上下文预算和结构化 memory |
| `episodic_memories` | 环境、领域特征、决策、执行、观测结果、案例质量和执行后评估结论 |
| `outcome_evaluations` | 决策生效后的多时间窗口效果评估：基线、观测、差值、verdict 与置信度 |

SQLite 使用 WAL、foreign key 和事务。`SessionLogger` 先写事件存储，再写 JSONL；每条 JSONL 同时携带 `event_id` 和 `sequence`，便于两种格式对账。

## 查询

```bash
.venv/bin/python memory_admin.py incomplete
.venv/bin/python memory_admin.py show <run_id>
.venv/bin/python memory_admin.py --db /path/to/db show <run_id>
.venv/bin/python memory_admin.py resolve-action <action_id> \
  --status succeeded --note "已通过 AP /status 确认生效"
```

`incomplete` 用于发现异常退出的运行；`show` 返回 checkpoint、完整有序事件和状态快照。

## 恢复边界

当前版本能识别未完成 run、重放事件并恢复最后安全协商边界。AP executor 下发已经接入 action journal：成功 action 复用缓存结果，明确失败最多尝试两次；网络异常标记为 `unknown` 并禁止自动重发。管理员需查询 AP `/status` 后用 `resolve-action` 消除恢复阻塞。

```bash
.venv/bin/python run_openclaw.py --resume-run <run_id> [原执行端点参数]
```

恢复投影包含初始 AP 状态、完整 transcript、当前 proposer/proposal/strategy、proposal 编号、已完成投票、投票游标和 Validator retry。恢复前会读取最新 AP 状态；可调参数、业务优先级或邻居集合变化时拒绝继续，QoS 数值自然波动不阻塞。

当前恢复粒度是协商安全边界，不恢复尚未完成的单个 LLM token/tool call。下一阶段需要：

1. 对非副作用工具建立结果缓存与可重放 step；
2. 增加恢复审批和 Dashboard 操作入口。

## Session Memory

`src/memory/session_memory.py` 提供确定性增量摘要器。它只压缩注入模型的上下文，不修改原始 transcript 或事件：

- 使用 `summarized_turns` 游标保证同一发言不会重复摘要；
- 将旧发言分类为 broadcast/proposal/vote/validator/message；
- proposal 保留参数 JSON 摘要，Validator 反馈优先保留；
- 默认保留最近 6 条原文；
- 默认每回合上下文硬上限 14000 字符；
- 摘要更新写入 `session_memories`，并合并进恢复 projection。

```bash
.venv/bin/python run_openclaw.py \
  --context-budget-chars 14000 \
  --context-recent-turns 6
```

本阶段暂不调用额外摘要模型，以确保摘要过程确定、快速、可测试。后续可以在相同 schema 上增加后台模型摘要，但必须保留确定性降级路径。

## Episodic Memory

run 结束时，`src/memory/episodic.py` 从事件和快照自动物化一个案例：

- 初始 AP 状态和拓扑签名；
- RSSI 链路、负载、重传、业务优先级、TX Power、EDCA 和 STA RSSI 特征；
- 最终决策、Validator 结果和 executor 响应；
- 最终真实观测存在时，记录吞吐、延迟和丢包前后差值；
- 基于成功、验收、执行和真实观测计算 `quality_score`。

检索先严格匹配拓扑签名，再按干扰 30%、负载 25%、优先级 15%、参数 20%、STA 10% 计算确定性相似度。提案阶段最多注入 3 个质量不低于 0.5 的案例摘要，并明确要求重新读取当前状态和调用工具验算。

```bash
.venv/bin/python memory_admin.py episodes --limit 20
.venv/bin/python memory_admin.py episodes --scene edca
.venv/bin/python memory_admin.py similar <run_id> --limit 5 --min-quality 0.5
```

## Outcome Evaluator

决策通过 Validator 并生效后，`src/memory/outcome.py` 在事件存储中登记多个评估窗口
（`--eval-windows`，默认 mock=`10,30`s、real=`60,300,900`s，`off` 关闭；coordinator
路径经 `MULTIAP_EVAL_WINDOWS` 环境变量透传）。每个窗口到期时与协商前基线比较：

- 指标：吞吐（iperf/user，相对变化）、延迟（相对变化，方向取反）、丢包（绝对
  百分点 / 5.0 归一），单指标得分截断在 [-1, 1]；
- 按业务优先级加权聚合（high=1.0 / medium=0.6 / low=0.3）：协商目标就是高优先级
  收益最大化，低优先级"让出信道"的小幅退化不会把整体判成恶化；
- 聚合得分 ≥ +0.05 → `improved`，≤ -0.05 → `degraded`，其间 `neutral`；
  指标覆盖率 < 50% → `inconclusive`；置信度由覆盖率和得分幅度共同决定。

评估结论确定性回写 episodic memory：最终 verdict 取最后一个有定论的窗口（最接近
稳态）；`improved` 按置信度加成质量分，`degraded` 把质量分封顶 0.2——低于提案注入
阈值 0.5，恶化方案不会再被当作高质量参考，同时生成恢复协商前参数的
`rollback_plan`（只含决策实际改过的字段）。**回滚只产出建议，不自动执行**：
真实下发仍需管理员经幂等 action journal 通道确认。

收割是惰性且不阻塞的：mock 保活循环内实时结算；`run_openclaw.py` 每次启动时
补收上一轮到期窗口（本轮提案即可检索到带真实效果结论的案例）；也可手动执行。
状态获取失败时窗口保持 pending 可重试。

```bash
.venv/bin/python memory_admin.py evaluate --server http://localhost:5001
.venv/bin/python memory_admin.py evaluations <run_id>
```

## 后续 Memory 分层

在 Event Store 稳定后依次增加：

1. ✅ Session Memory：阶段增量摘要和上下文预算；
2. ✅ Episodic Memory：一次协商的环境、行动、反馈和结果案例；
3. ✅ Outcome Evaluator：执行后多时间窗口真实效果评估、质量修订与回滚建议；
4. Semantic Memory：从多个案例归纳、带证据和置信度的规律；
5. Consolidation：带锁、门控、冲突检测和过期管理的后台整理。

任何长期记忆都只作为提案参考，当前实时状态和确定性 Validator 始终具有更高优先级。
