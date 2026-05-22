# 可用工具

在提案和验算阶段，你可以调用以下工具辅助决策。
**调用工具时直接使用工具名和参数，等待返回结果后再给出最终方案。**
执行提案或投票时，必须先调用 `get_latest_ap_states` 获取最新状态；
随后所有计算和验算都以该工具返回的最新状态为依据。

---

## get_latest_ap_states

**位置**：`src/tools/registry.py` → 绑定当前状态源
**适用阶段**：所有提案阶段、所有投票验算阶段、提案修订阶段

获取所有 AP 的最新参数状态，包括 TX Power、EDCA 参数、信道占用率、
重传率、邻居 RSSI、STA RSSI、噪声、吞吐、时延和丢包等指标。

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

## compute_sr_feasible_ranges

**位置**：`src/tools/sr.py` → `compute_feasible_ranges()`
**适用阶段**：Co-SR 或联合协商的提案阶段

计算每个 AP 的 TX Power 可行区间。区间来自法定功率上下限、
STA RSSI 安全下界和 CCA 上界。SINR 是 AP 间耦合约束，候选方案仍需继续评估。

**参数**：无（工具自动读取当前 AP 状态）

**返回字段说明**：
- `ranges`：每个 AP 的 `current_dbm` / `min_dbm` / `max_dbm` / 约束原因
- `candidate_hints`：适合进一步评估的候选提示，例如 `minimal_necessary_drop`
- `notes`：使用区间时的注意事项

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
  }
}
```

**返回字段说明**：
- `valid`：候选方案整体是否合法
- `score`：`total_power_drop_db` / `max_single_ap_drop_db` / `min_sta_rssi_margin_db` 等
- `per_ap`：每个 AP 的 `cca_ok` / `sinr_ok` / `sta_rssi_ok` / `errors`

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

**位置**：`src/tools/edca.py` → `validate()`
**适用阶段**：投票验算阶段，或提案方在提交前自检

检查各 AP 的 EDCA 参数是否在 IEEE 802.11 合法范围内：
- CWmin ∈ [3, 1023]
- CWmax ∈ [7, 1023]
- AIFSN ∈ [1, 15]
- CWmax > CWmin

**参数**：
```json
{
  "proposed_edca": {
    "ap1": {"CWmin": 15, "CWmax": 63, "AIFSN": 3},
    "ap2": {"CWmin": 7,  "CWmax": 31, "AIFSN": 3},
    "ap3": {"CWmin": 7,  "CWmax": 31, "AIFSN": 4}
  }
}
```

**返回字段说明**：

- 每个 AP（`ap1` / `ap2` / `ap3`）：`valid` / `errors` / 回传的 `CWmin` / `CWmax` / `AIFSN`
- `effectiveness.per_ap.<ap_id>`：
  - `recommended_level`：工具根据当前 busy/retry 判断的推荐等级
  - `cwmin_delta_vs_rec`：提案 CWmin 与推荐值之差（负数表示更激进）
  - `estimated_p_collision`：简化碰撞概率估算
  - `warnings`：合理性警告列表（空=无异常）
- `effectiveness.fairness.warnings`：AIFSN 差值 ≥ 3 时触发，说明低 AIFSN AP 将系统性占优
- `effectiveness.all_ok`：所有 AP 及公平性均无警告时为 true

**投票建议**：若 `valid=false` 或 `effectiveness.warnings` 非空，应说明具体问题并表示不同意。

---

## 调用时机速查

| 阶段 | 你的角色 | 应调用的工具 |
|------|---------|------------|
| 提案（Co-SR） | 提案方 | `get_latest_ap_states` → `analyze_sr_interference` → `compute_sr_feasible_ranges` → `rank_sr_candidates` / `evaluate_sr_candidate` |
| 提案（Co-EDCA） | 提案方 | `get_latest_ap_states` → 自行提出 EDCA 候选 → `validate_edca_proposal` 自检 |
| 提案（联合） | 提案方 | `get_latest_ap_states` → Co-SR 分析/候选工具 + `validate_edca_proposal` |
| 投票（Co-SR） | 投票方 | `get_latest_ap_states` → `evaluate_sr_candidate`（传入提案中的功率值） |
| 投票（Co-EDCA） | 投票方 | `get_latest_ap_states` → `validate_edca_proposal`（传入提案中的 EDCA 参数） |
| 投票（联合） | 投票方 | `get_latest_ap_states` → `evaluate_sr_candidate` + `validate_edca_proposal` |
