# 质疑反思模块与迭代模块：目标制定与架构部署方案

> 状态：**已全部实施（阶段 1–7 完成，2026-07-07）**。验收结果：R1–R6、I1–I6
> 全部落地并有测试锁定；实际 schema 版本为 v16/v17（矛盾账本 + 校准表 + goals）。
> I5 基准（固定种子 30 场景）：单轮达成率 50.0% → 迭代达成率 86.7%（+36.7%）。
> 两条新红线已并入 `docs/memory-module.md` 安全原则（第 9、10 条）。
> 本文档定义两个模块的验收目标、架构落点和分阶段路线。
> 记忆体系现状见 `docs/memory-module.md`，演进历史见 `docs/memory-architecture.md`。
>
> **阶段 8 补丁（2026-07-09，真实 ns-3 联调触发）**：一次真实协商暴露出
> R4 的结构性空当——矛盾账本只核对"复用的历史记忆"，一次协商如果完全没
> 依赖任何历史记忆（当场想出的新参数组合），预测-实测核账就完全不会发生，
> 不管协商结果多离谱都不会留下任何校准证据；同一次事故也让 Validator 补了
> 第三层"自伤幅度门"（见下方"六、事后补丁"）。两个补丁都不改 schema、
> 不新增开关，复用既有的 `reconciliation`/`memory_contradictions` 表和
> MULTIAP_REFLECTION 总开关。

## 一、动机与现状差距

现有记忆体系（会话 / 案例 / 语义 / 效果评估 / 整理 / 可观测）已经解决了
"记住并评估"，但有两个结构性空白：

1. **质疑是被动的。** 记忆带 `quality_score` 和 `outcome_confidence`，提示词里
   写了"仅供参考"，但 Agent 拿到记忆后没有任何机制去检验它是否仍然适用。
   记忆一旦被评为高质量，就会一直以同样的权重被注入，哪怕环境早已漂移、
   哪怕后续多次观测已经和它的结论相悖。
2. **迭代是隐式的。** 每轮协商各自独立，效果评估只回答"这次决策好坏"，
   不回答"离某个目标还有多远"。失败后的下一轮协商不会携带结构化的归因
   （上次改了什么 → 观测到什么偏差 → 为什么 → 这次换什么假设），Agent 只能
   靠案例相似度间接受益。

两个模块的关系是一个闭环：**反思**给记忆定信任、发现"该改进的问题"；
**迭代**围绕目标做多轮尝试、产生新证据；新证据回头更新信任。

## 二、实用性约束（继承自现有工程实践）

所有目标必须满足以下硬约束，否则不予验收：

| 约束 | 来源实践 |
|---|---|
| 确定性优先：信任分、门控、进度评分、停机判断同输入必同输出 | 摘要器 / 检索 / 评估的既有原则 |
| 不新增必经 LLM 调用；LLM 只用于可选叙事，必须有确定性降级路径 | `llm_backend.py` 模式 |
| 事件溯源：一切结论可从 `run_events` 重放推导，不物理删除任何记忆 | Event Store / 归档实践 |
| 可开关降级：环境变量一键关闭新模块，行为回退到当前版本 | `MULTIAP_EVENT_STORE=0` 模式 |
| 幂等：重复执行调度 / 记账 / 整理不产生重复副作用 | action journal / 评估租约实践 |
| 可观测：新增状态进入 `memory_health` 聚合和 Dashboard 面板 | 迭代五可观测性实践 |
| 可管理：新增对象有 `memory_admin.py` 子命令 | 现有 CLI 实践 |
| 每阶段独立提交 + 独立测试文件 | "第 N 步 / 共 M 步"提交实践 |

新增两条设计红线（追加到 `memory-module.md` 的安全原则）：

9. **反思只降权不删除**：质疑的最强后果是把记忆移入隔离区（停止注入），
   永不物理删除；隔离可经再验证解除。
10. **迭代不越权执行**：目标驱动的每一轮尝试仍走完整的
    协商 → Validator → 幂等 journal 链路；停机准则触发后只标记、只升级，
    绝不自动加轮次或自动回滚。

## 三、质疑反思模块：目标

### 架构部署（四个切入点，全部在现有链路的缝隙上，不改主流程拓扑）

```text
                     ┌────────────────────────────────────┐
                     │ ① 数据层：信任模型（schema v16）        │
                     │   trust = confidence × 时效衰减       │
                     │           × 矛盾惩罚                  │
                     └────────────────────────────────────┘
 召回时 ──────────────► ② 门控：find_similar_episodes /
                          find_matching_rules 按 trust 过滤，
                          隔离区记忆不注入
 注入时 ──────────────► ③ 假设化格式：workspace.py 注入
                          "前提 + 可证伪预测 + 信任分 + 最近验证时间"
 评估后 ──────────────► ④ 矛盾账本：outcome.py 收割窗口时，
                          比对被依赖记忆的预测 vs 实际 verdict，
                          确定性记账并回写 trust
```

### 验收目标

**R1 — 信任分可复算。** 每条案例/规则有确定性信任分：
`trust = outcome_confidence × freshness(距最近验证的时间) × contradiction_penalty(矛盾次数)`。
同一数据库状态下重复计算结果逐位一致；有单元测试锁定衰减曲线和惩罚系数。

**R2 — 隔离区零注入。** trust 低于阈值的记忆进入隔离区（新状态，非删除）；
测试断言：隔离记忆在提案注入、规则匹配两条路径上出现次数为 0；
`memory_admin.py quarantine` 可列出、`revalidate` 可解除。

**R3 — 依赖可审计。** 提案阶段注入了哪些记忆（memory_id、当时的 trust）作为
`memory_reliance` 事件落入 `run_events`；`memory_admin.py show <run_id>` 能
回答"这次决策参考了哪几条记忆，事后各自被证实还是证伪"。

**R4 — 矛盾账本闭环。** 效果评估收割时，被依赖记忆的预测方向与实际 verdict
相悖 → 矛盾计数 +1 并重算 trust；连续矛盾达到阈值 → 自动进隔离区。
全程确定性、走既有评估租约，满足反馈隔离红线。

**R5 — 校准可见（模块实用性的量化验收）。** Dashboard `/memory` 面板新增
校准指标：注入时信任分 vs 事后 verdict 的一致率（分桶命中率）。这是判断
"反思模块是否真的有用"的唯一标准——如果高信任记忆的事后证实率并不比
低信任记忆高，说明信任模型无效，需要校准参数。

**R6 — 零额外时延预算。** 门控与信任计算是纯 SQL + 内存运算，
不新增 LLM 调用；单次协商新增耗时 < 100ms（mock 场景测试可断言）。

### 明确不做（本期）

- 不做 LLM 主动质疑回合（每轮协商多一次 LLM 调用，违反时延约束，
  且效果无法确定性验收）；质疑先用确定性的矛盾账本实现，LLM 叙事后补。
- 不做主动探测试验（故意偏离规则做对照）——依赖 ns-3 桥完成后再评估。

## 四、迭代模块：目标

### 架构部署（在协商轮之上加"目标层"，复用评估窗口机制）

```text
┌─ Goal（schema v16 新表）────────────────────────────────┐
│ metric + target + baseline + budget(最大尝试数/时限) + │
│ status(active/achieved/blocked/abandoned)              │
└──────────────┬─────────────────────────────────────────┘
               │ 1 goal : N attempts（parent_attempt_id 成链）
        ┌──────▼──────┐   每次 attempt = 一次完整协商 run
        │ run_openclaw │   （run_id 关联 goal_id，恢复机制天然覆盖）
        └──────┬──────┘
               │ 复用 schedule_outcome_evaluations 的窗口
        ┌──────▼──────────────────────────┐
        │ 窗口到期：既评决策好坏，也评目标进度 │
        │ 未收敛 → 生成确定性归因摘要，        │
        │ 注入下一次 attempt 的提案提示        │
        └─────────────────────────────────┘
```

### 验收目标

**I1 — 目标是一等公民。** Goal 持久化在 Event Store（schema v16）；来源两种：
管理员 `memory_admin.py goal create` 下发，或确定性触发规则自动创建
（某指标连续 N 个评估窗口越界）。崩溃后 `--resume-run` 能恢复目标上下文。

**I2 — 迭代链完整可追溯。** 每次 attempt 记录 `parent_attempt_id` 和结构化
归因（上次动作 → 观测差值 → 确定性归因分类 → 本次假设）；归因基于既有
`evaluate_deltas` 的差值数据生成，不依赖 LLM。`memory_admin.py goal show`
能打印整条链。

**I3 — 归因进提示词。** 下一次 attempt 的提案阶段注入上一次的归因摘要
（格式与案例注入并列，走 `workspace.py` 同一通道），Agent 被要求针对
归因提出**不同的**假设，而不是重复同样的参数。

**I4 — 停机准则可测试。** 三种停机条件全部有模拟测试：
预算耗尽 → `blocked`；参数振荡（同一参数来回改，确定性检测）→ `blocked`；
目标达成且在下一窗口保持 → `achieved`。任何路径都不允许无限循环。

**I5 — 实效基准（模块实用性的量化验收）。** 建立一个 mock 基准场景
（复用 MockTelemetryFeeder，固定随机种子）：同一初始状态下，
"目标驱动多轮迭代" 相比 "单轮协商" 的目标指标达成率必须有可复现的提升。
这个对比实验脚本进入测试套件，作为迭代模块存在价值的持续证明。

**I6 — 与反思模块闭环。** 目标达成的迭代链参与语义规则归纳（支持度加成）；
失败链上被依赖的记忆走 R4 矛盾账本。两模块共用 schema v16 迁移，一次到位。

### 明确不做（本期）

- 不做多目标调度/优先级抢占——单活跃目标起步，避免编排复杂度爆炸。
- 不做目标自动执行回滚——沿用"回滚只产出建议"红线。

## 五、分阶段路线（每阶段独立提交 + 测试）

| 阶段 | 内容 | 主要落点 | 验收目标 |
|---|---|---|---|
| 1 | schema v16 迁移：信任字段、矛盾账本、goals / attempts 表；信任分纯函数 | `src/persistence/event_store.py`、新建 `src/memory/reflection.py` | R1 |
| 2 | 召回门控 + 隔离区 + admin 子命令 | `episodic.py`、`semantic.py`、`memory_admin.py` | R2、R6 |
| 3 | 假设化注入格式 + `memory_reliance` 事件 | `workspace.py`、`orchestration.py` | R3 |
| 4 | 评估收割接矛盾账本 + 校准指标进 observability/Dashboard | `outcome.py`、`observability.py`、`dashboard` | R4、R5 |
| 5 | Goal 对象 + goal-scoped run + 进度评分 | 新建 `src/memory/goals.py`、`run_openclaw.py` | I1 |
| 6 | 迭代链归因 + 提示注入 + 停机准则 | `goals.py`、`workspace.py`、`orchestration.py` | I2、I3、I4 |
| 7 | 基准对比实验 + 规则归纳/矛盾账本闭环 | `tests/`、`consolidation.py` | I5、I6 |

阶段 1–4 只影响"注入什么记忆"，不改变协商行为本身，风险低；
阶段 5–7 引入新行为，需配 mock 场景回归。全部开关：
`MULTIAP_REFLECTION=0`、`MULTIAP_GOALS=0` 可整体禁用回退。

## 六、阶段 8 事后补丁（真实 ns-3 联调触发，2026-07-09）

跑了三轮真实 ns-3 协商 + 挂 goal 的迭代链之后，实测暴露三个问题，均已修复
（commit `fabc5df`、`b73a273` 及本次）：

1. **迭代模块停机准则漏了一条路径**（I4）：一次 attempt 如果在产出决策前
   就失败（如 LLM context overflow 导致提案解析失败），不会登记评估窗口，
   `refresh_goal_after_evaluation` 永远不会被触发，预算耗尽即便已经打满
   也不会转 `blocked`，目标卡死在 `active`。修法：`goals.record_attempt_result`
   的失败分支里补跑一次 `refresh_goal_after_evaluation`（成功分支不变，
   避免在评估窗口结算前抢先误判 blocked，见 `src/memory/goals.py`）。

2. **Validator 只做参数合法性 + 优先级排序，没有幅度检查**：一次 Co-EDCA
   提案把 low 优先级 AP 的 CWmin/AIFSN 拉到 [32,7]，排序完全合规
   （low 本该更保守），但幅度极端到把自己的信道抢占能力压到接近零——
   实测 iperf 吞吐从 11.3Mbps 崩到 0，Validator 全程放行。补的是 Validator
   第三层"自伤幅度门"：Co-EDCA 用闭式估算的信道抢占份额跌幅
   （`src/tools/edca.py: predict_access_share/detect_self_harm`），
   Co-SR 复用已有的 STA RSSI 安全下界逻辑但接上强制网关
   （`src/tools/sr.py: detect_self_harm`），都在 `src/validator.py` 里
   接成必过的第三层检查，不依赖真实观测。EDCA 一侧留了"邻居确有 SLA
   违规"的正当牺牲例外，Co-SR 一侧是硬性安全底线不设例外。

3. **反思模块对"当场新决策"完全失明**：矛盾账本（R4）只对*复用的历史
   记忆*做预测-实测核账；本次事故那类全新参数组合，从未进入任何账本，
   哪怕协商结果离谱到吞吐归零，也不会给 R5 校准表留下任何证据，更不会
   有任何机制提示"这类决策该被质疑"——除非挂了 goal 靠迭代链下一次
   attempt 才可能被归因，日常协商（不挂 goal）出同样的事故完全没有后续。
   补的是 `src/memory/reflection.py: predict_decision_verdicts /
   reconcile_decision_predictions`：复用 Validator 自伤门已经算出的闭式
   估算（EDCA 份额比 / Co-SR 功率 delta）反推这次决策的方向性预测，
   跟评估窗口的 per-AP 实测得分核账，写进已有的 `reconciliation` 表
   （`memory_kind="decision_prediction"`，`trust_at_injection=None`）。
   接入点是 `outcome.py: apply_evaluation_to_episode`，跟 R4 的
   `reconcile_memory_reliance` 并列执行，**不管这次协商有没有依赖历史
   记忆、有没有挂 goal 都会跑**——不写矛盾账本（预测不是可隔离的记忆对象，
   红线 9 的隔离动作没有作用对象），只写 R5 校准表；`calibration_report`
   新增按 `memory_kind` 的 `by_kind` 明细，把"记忆复用是否可信"和
   "新决策的预测准不准"分开看。

用真实事故的原始数据（run_id=`ea70736a`）回放验证：新 Validator 正确拒绝
了当时被放行的决策；`predict_decision_verdicts` 对同一份数据算出的方向
（ap1 degraded / ap2 improved / ap3 neutral）跟实测完全吻合。
