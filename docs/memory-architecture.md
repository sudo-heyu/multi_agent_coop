# Agent Memory 架构与实施状态

## 当前阶段

第一阶段已建立本地 SQLite Event Store。它与 JSONL 双写，不改变当前 OpenClaw 协商行为，为后续任务恢复、案例记忆、效果评估和检索提供持久化基础。

默认数据库：`logs/agent_memory.sqlite3`。

可通过环境变量覆盖：

```bash
MULTIAP_EVENT_DB=/path/to/agent.sqlite3 .venv/bin/python run_openclaw.py
MULTIAP_EVENT_STORE=0 .venv/bin/python run_openclaw.py  # 仅调试时禁用
```

## Schema v5

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
| `episodic_memories` | 环境、领域特征、决策、执行、观测结果和案例质量 |

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

1. Outcome Evaluator：执行后按多个时间窗口采集真实效果；
2. 对非副作用工具建立结果缓存与可重放 step；
3. 增加恢复审批和 Dashboard 操作入口。

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

## 后续 Memory 分层

在 Event Store 稳定后依次增加：

1. Session Memory：阶段增量摘要和上下文预算；
2. Episodic Memory：一次协商的环境、行动、反馈和结果案例；
3. Outcome Evaluator：执行后多时间窗口真实效果评估；
4. Semantic Memory：从多个案例归纳、带证据和置信度的规律；
5. Consolidation：带锁、门控、冲突检测和过期管理的后台整理。

任何长期记忆都只作为提案参考，当前实时状态和确定性 Validator 始终具有更高优先级。
