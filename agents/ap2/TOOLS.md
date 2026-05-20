# 可用工具

在提案和验算阶段，你可以调用以下工具辅助决策。
**调用工具时直接使用工具名和参数，等待返回结果后再给出最终方案。**

---

## compute_sr_recommendations

**位置**：`src/tools/sr.py` → `compute_all()`
**适用阶段**：Co-SR 或联合协商的提案阶段

计算满足三重物理约束的推荐发射功率：
- CCA < −82 dBm（邻居 AP 不触发 CCA 检测）
- SINR ≥ 15 dB（STA 链路质量下界）
- STA RSSI ≥ −75 dBm（关联安全下界）

工具从当前最高功率向下扫描，返回满足全部约束的最高可行统一功率，
以及每个 AP 的当前功率 → 推荐功率的变化量。

**参数**：无（工具自动读取当前 AP 状态）

**返回字段说明**：
- `interference_matrix`：AP 间干扰 RSSI 及等级（strong / moderate / weak）
- `feasible`：是否存在可行功率
- `recommended_uniform_dbm`：推荐的统一功率值（dBm）
- `recommendations`：每个 AP 的 `current_dbm` / `recommended_dbm` / `delta_db`
- `validation`：推荐功率下各 AP 的 `cca_ok` / `sinr_ok` / `sta_rssi_ok`

---

## compute_edca_recommendations

**位置**：`src/tools/edca.py` → `compute_all()`
**适用阶段**：Co-EDCA 或联合协商的提案阶段

根据各 AP 的 `channel_busy_ratio` 和 `tx_retries_ratio` 判断拥塞等级，
映射到推荐的 EDCA 参数组合。

拥塞等级判定：
- `low`：busy < 40% 且 retries < 8%
- `medium`：busy < 60% 且 retries < 15%
- `high`：busy < 75% 或 retries < 25%
- `critical`：busy ≥ 75% 且 retries ≥ 25%

**参数**：无（工具自动读取当前 AP 状态）

**返回字段说明**：每个 AP 的 `congestion_level` / `CWmin` / `CWmax` / `AIFSN` / `valid`

---

## validate_sr_proposal

**位置**：`src/tools/sr.py` → `compute_validation()`
**适用阶段**：投票验算阶段，或提案方在提交前自检

验证提案中各 AP 的 TX Power 是否满足 CCA / SINR / STA RSSI 三重约束。
输入你从提案 JSON 中读取的功率值，工具返回每个 AP 的详细验算结果。

**参数**：
```json
{
  "proposed_powers": {
    "ap1": 10.0,
    "ap2": 12.0,
    "ap3": 16.0
  }
}
```

**返回字段说明**：每个 AP 的 `cca_max_dbm` / `cca_ok` / `sinr_db` / `sinr_ok` / `sta_rssi_dbm` / `sta_rssi_ok` / `valid` / `errors`

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

**返回字段说明**：每个 AP 的 `valid` / `errors` 及回传的参数值

---

## 调用时机速查

| 阶段 | 你的角色 | 应调用的工具 |
|------|---------|------------|
| 提案（Co-SR） | 提案方 | `compute_sr_recommendations` → （可选）`validate_sr_proposal` 自检 |
| 提案（Co-EDCA） | 提案方 | `compute_edca_recommendations` → （可选）`validate_edca_proposal` 自检 |
| 提案（联合） | 提案方 | 两个 compute 工具均调用 |
| 投票（Co-SR） | 投票方 | `validate_sr_proposal`（传入提案中的功率值） |
| 投票（Co-EDCA） | 投票方 | `validate_edca_proposal`（传入提案中的 EDCA 参数） |
| 投票（联合） | 投票方 | 两个 validate 工具均调用 |
