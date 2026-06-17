# 可用工具

在提案和验算阶段，你可以调用以下 OpenClaw MCP 工具（multiap-tools）辅助决策。
**调用工具时直接使用工具名和参数，等待返回结果后再给出最终方案。**
执行提案或投票时，必须先调用 `get_latest_ap_states` 获取最新状态；
随后所有计算和验算都以该工具返回的最新状态为依据。

---

## get_latest_ap_states

**位置**：`src/tools/registry.py` → 绑定当前状态源
**适用阶段**：所有提案阶段、所有投票验算阶段、提案修订阶段

获取所有 AP 的最新参数状态，包括 TX Power、EDCA 参数、信道利用率（Data rate-to-bandwidth ratio）、
重传率、邻居 RSSI、STA RSSI、噪声、吞吐（iperf/user 双路及各自 AC 类型）、时延和丢包等指标。

**参数**：无

**返回字段说明**：
- `ok`：是否成功获取最新状态
- `source`：状态来源，真实 AP 模式通常为 `state_getter`，mock 模式为 `current_snapshot`
- `ap_states`：最新的 `ap1` / `ap2` / `ap3` 全局状态

---

## analyze_sr_interference

**位置**：`src/tools/sr.py` → `analyze_interference()`
**适用阶段**：Co-SR 或联合协商的提案阶段

分析当前 AP 间干扰关系，只返回事实和风险，不给最终功率建议。

**参数**：无（工具自动读取当前 AP 状态）

**返回字段说明**：
- `interference_matrix`：AP 间干扰 RSSI 及 strong / moderate / weak 分级
- `strong_links` / `moderate_links`：强/中等干扰链路
- `primary_interferers`：主要干扰源排序
- `primary_victims`：主要受害 AP 排序
- `co_sr_triggered`：是否触发 Co-SR

---

## select_sr_concurrent_groups

**位置**：`src/tools/sr.py` → `select_concurrent_groups()`
**适用阶段**：Co-SR 或联合协商的提案阶段

先枚举可行空间复用并发组，再给出组内推荐 TX Power。用于部分并发：
例如最远的 `ap1` / `ap3` 可进入 `concurrent_group`，中间强干扰 `ap2`
放入 `non_concurrent_aps`，不参与同一并发时刻。

**参数**：
```json
{"min_group_size": 2}
```

**返回字段说明**：
- `best_group`：排序后的最佳并发组，包含 `concurrent_group` / `non_concurrent_aps` / `recommended_powers`
- `valid_groups`：所有可行并发组
- `all_groups`：包含不可行组及其原因

**提案要求**：若采用部分并发，最终 JSON 除每个 AP 的 `tx_power_dbm` 外，还应包含：
```json
{"_sr": {"concurrent_group": ["ap1", "ap3"], "non_concurrent_aps": ["ap2"]}}
```

---

## compute_sr_feasible_ranges

**位置**：`src/tools/sr.py` → `compute_feasible_ranges()`
**适用阶段**：Co-SR 或联合协商的提案阶段

计算每个 AP 的 TX Power 可行区间。区间来自法定功率上下限、
STA RSSI 安全下界和 CCA 上界。SINR 是 AP 间耦合约束，候选方案仍需继续评估。

**参数**：无（工具自动读取当前 AP 状态）

**返回字段说明**：
- `ranges`：每个 AP 的 `current_dbm` / `min_dbm` / `max_dbm` / `min_int_dbm` / `max_int_dbm` / 约束原因
- `integer_power_required`：恒为 `true`，提醒功率调整量必须为整数 dB
- `candidate_hints`：适合进一步评估的候选提示（已取整为整数 dBm），例如 `minimal_necessary_drop`
- `notes`：使用区间时的注意事项

**整数约束**：`tx_power_dbm` 只能取整数（参考 `min_int_dbm` / `max_int_dbm`）；功率降低量必须是整数 dB。

---

## evaluate_sr_candidate

**位置**：`src/tools/sr.py` → `evaluate_candidate()`
**适用阶段**：Co-SR 投票验算，或提案方提交前自检

评估一个候选 TX Power 方案是否满足 CCA / SINR / STA RSSI 三重约束，
并返回总降功率、最大单 AP 降功率、STA RSSI 余量等代价指标。

**参数**：
```json
{
  "proposed_powers": {
    "ap1": 7.0,
    "ap2": 7.0,
    "ap3": 8.0
  },
  "concurrent_group": ["ap1", "ap3"]
}
```

**传参规则（重要）**：
- **提案 / 提交前自检**：必须显式传入 `proposed_powers`（你打算提出的功率）。部分并发时还必须传入 `concurrent_group`。此阶段没有"当前提案"可供回填，省略会被当成空提案、退化为验算当前功率，结果无意义。
- **投票验算**：协调者会在投票指令中给出当前提案的完整参数。你必须把提案中各 AP 的 `proposed_powers`（部分并发时连同 `concurrent_group`）显式填入工具参数——本架构下工具不会自动回填提案。

**返回字段说明**：
- `valid`：候选方案整体是否合法（功率调整量非整数 dB 时为 `false`）
- `score`：`total_power_drop_db` / `max_single_ap_drop_db` / `min_sta_rssi_margin_db` 等
- `per_ap`：每个 AP 的 `cca_ok` / `sinr_ok` / `sta_rssi_ok` / `delta_is_integer` / `errors`

**注意**：候选 `tx_power_dbm` 必须为整数，相对当前功率的调整量必须是整数 dB，否则 `valid=false`。

---

## rank_sr_candidates

**位置**：`src/tools/sr.py` → `rank_candidates()`
**适用阶段**：Co-SR 或联合协商的提案阶段

对多个候选 TX Power 方案排序，帮助比较协商方案。

**参数**：
```json
{
  "candidates": {
    "balanced": {"ap1": 7.0, "ap2": 7.0, "ap3": 8.0},
    "protect_ap3": {"ap1": 6.5, "ap2": 7.0, "ap3": 9.0}
  },
  "objective": "balanced"
}
```

`objective` 可选：`balanced` / `minimize_total_drop` / `minimize_max_drop` / `maximize_sta_margin`

**返回字段说明**：
- `best`：排序第一的候选
- `ranked_candidates`：所有候选的合法性、功率、代价和错误原因

---

## validate_edca_proposal

**位置**：`src/tools/edca.py`
**适用阶段**：投票验算阶段，或提案方在提交前自检

执行两项校验：

**① 范围合规（IEEE 802.11 标准）**
- CWmin ∈ [3, 1023]
- CWmax ∈ [7, 1023]
- AIFSN ∈ [1, 15]
- CWmax > CWmin

**② 优先级排序**（核心 QoS 约束）

各 AP 状态中含 `traffic_priority` 字段（`high` / `medium` / `low`），表示该 AP 所承载业务的优先级。优先级决定 EDCA 参数的调整方向：

- **高优先级（high）**：应使用**更小**的 CWmin、CWmax、AIFSN。更小的退避窗口和更短的 AIFS 间隔，使高优先级 AP 比低优先级 AP 更早开始竞争并更早接入信道，从而降低时延、保障 QoS。
- **低优先级（low）**：应使用**更大**的 CWmin、CWmax、AIFSN，主动放慢竞争节奏，让出信道机会给高优先级业务。
- **中优先级（medium）**：参数介于两者之间。

工具强制要求不同优先级 AP 的参数满足单调性：
```
high.CWmin ≤ medium.CWmin ≤ low.CWmin
high.AIFSN ≤ medium.AIFSN ≤ low.AIFSN
```
同优先级的多个 AP 之间无顺序约束。

**参数**：
```json
{
  "proposed_edca": {
    "ap1": {"CWmin": 3,  "CWmax": 15, "AIFSN": 2},
    "ap2": {"CWmin": 7,  "CWmax": 31, "AIFSN": 3},
    "ap3": {"CWmin": 15, "CWmax": 63, "AIFSN": 6}
  }
}
```

**传参规则（重要）**：
- **提案 / 提交前自检**：必须显式传入 `proposed_edca`（你打算提出的 EDCA 参数）。此阶段没有"当前提案"可供回填，省略会直接报"参数缺失"。
- **投票验算**：协调者会在投票指令中给出当前提案的完整参数。你必须把提案中各 AP 的 `proposed_edca` 显式填入工具参数——本架构下工具不会自动回填提案。

**返回字段说明**：

- 每个 AP（`ap1` / `ap2` / `ap3`）：`valid` / `errors` / 回传的 `CWmin` / `CWmax` / `AIFSN`
- `effectiveness.per_ap.<ap_id>`：
  - `traffic_priority`：该 AP 的业务优先级
  - `cwmin` / `aifsn`：提案参数（用于排序校验）
- `effectiveness.priority_ordering.warnings`：优先级排序违规时触发（高优先级参数 > 低优先级参数）
- `effectiveness.all_ok`：排序校验全部通过时为 true

**投票建议**：若 `valid=false` 或 `effectiveness.priority_ordering.ok=false`，应说明具体问题并表示不同意。

---

## 调用时机速查

| 阶段 | 你的角色 | 应调用的工具 |
|------|---------|------------|
| 提案（Co-SR） | 提案方 | `get_latest_ap_states` → `analyze_sr_interference` → `select_sr_concurrent_groups` → `evaluate_sr_candidate` |
| 提案（Co-EDCA） | 提案方 | `get_latest_ap_states` → 自行提出 EDCA 候选 → `validate_edca_proposal` 自检 |
| 提案（联合） | 提案方 | `get_latest_ap_states` → Co-SR 分析/候选工具 + `validate_edca_proposal` |
| 投票（Co-SR） | 投票方 | `get_latest_ap_states` → `evaluate_sr_candidate`（传入提案中的功率值） |
| 投票（Co-EDCA） | 投票方 | `get_latest_ap_states` → `validate_edca_proposal`（传入提案中的 EDCA 参数） |
| 投票（联合） | 投票方 | `get_latest_ap_states` → `evaluate_sr_candidate` + `validate_edca_proposal` |
