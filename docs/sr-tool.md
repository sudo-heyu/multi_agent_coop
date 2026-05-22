# Co-SR 计算工具

模块路径：`src/tools/sr.py`

---

## 作用

根据 AP 间 RSSI 感知干扰强度，连续求解满足三重约束的最优 TX Power，
供 orchestrator 在第二阶段（提案）提供给 LLM。

---

## 物理模型

工具基于 RSSI 线性模型，**不依赖坐标**：

- 路径损耗在 dB 尺度线性：AP_j 功率变化 Δ dBm → AP_i 处接收也变化 Δ dBm
- STA_i 处来自 AP_j 的干扰用 AP_i 处 `neighbor_rssi_dbm[j]` 保守近似（最坏情况）

---

## 三重约束

| 约束 | 阈值 | 含义 |
|---|---|---|
| CCA ≤ 阈值 | **-82.0 dBm** | 邻居 AP 信号不触发 OBSS 边界检测，允许空间复用 |
| SINR ≥ 阈值 | **15.0 dB** | STA 链路质量下界，低于此值吞吐量急剧下降 |
| STA RSSI ≥ 安全下界 | **-75.0 dBm** | 降功率后己方 STA 不断连 |

---

## 干扰强度分级

| 等级 | neighbor_rssi_dbm | 含义 |
|---|---|---|
| `strong` | ≥ -70 dBm | 严重干扰，Co-SR 触发必要条件 |
| `moderate` | ≥ -80 dBm | 中等干扰，可选优化 |
| `weak` | < -80 dBm | 弱干扰，无需调整 |

---

## 优化目标

工具不再逐 1 dBm 扫描，也不求统一功率。它在连续 dBm 空间中求解：

```text
minimize Σ (new_tx_power_i - current_tx_power_i)^2
subject to:
  CCA < -82 dBm
  SINR >= 15 dB
  STA RSSI >= -75 dBm
  TX_POWER_MIN <= new_tx_power_i <= min(current_tx_power_i, TX_POWER_MAX)
```

也就是在满足空间复用和链路安全约束的前提下，求每个 AP 的最小必要功率调整。

---

## 函数接口

### `classify_interference(rssi_dbm) → str`

判断单对 AP 间干扰强度等级。

```python
from src.tools.sr import classify_interference

classify_interference(-65.0)  # → "strong"
classify_interference(-75.0)  # → "moderate"
classify_interference(-85.0)  # → "weak"
```

---

### `compute_interference_matrix(ap_states) → dict`

构建 AP 间当前干扰矩阵。

```python
from src.tools.sr import compute_interference_matrix

matrix = compute_interference_matrix(ap_states)
# {
#   "ap1->ap2": {"rssi_dbm": -65.0, "level": "strong"},
#   "ap1->ap3": {"rssi_dbm": -72.0, "level": "moderate"},
#   ...
# }
```

---

### `analyze_interference(ap_states) → dict`

分析 Co-SR 干扰关系，返回强/中/弱链路列表、主要干扰源和受害 AP 排名。工具只描述无线环境，不给出最终功率决策。

```python
from src.tools.sr import analyze_interference

result = analyze_interference(ap_states)
# {
#   "interference_matrix": {"ap1->ap2": {"rssi_dbm": -65.0, "level": "strong"}, ...},
#   "strong_links":   [{"source_ap": "ap2", "victim_ap": "ap1", "rssi_dbm": -65.0, "level": "strong"}, ...],
#   "moderate_links": [...],
#   "primary_interferers": [{"ap_id": "ap2", "score": 3, "strong_links": 1, "moderate_links": 0}, ...],
#   "primary_victims":     [{"ap_id": "ap1", "score": 3, ...}, ...],
#   "co_sr_triggered": True,
#   "summary": {"strong_link_count": 1, "moderate_link_count": 2,
#               "strong_threshold_dbm": -70.0, "cca_threshold_dbm": -82.0},
# }
```

`primary_interferers` / `primary_victims` 按 `strong×3 + moderate` 降序排列，`co_sr_triggered` 为 `True` 当且仅当存在强干扰链路。

---

### `compute_feasible_ranges(ap_states) → dict`

计算每个 AP 在满足 CCA 和 STA RSSI 约束下的连续 TX Power 可行区间。SINR 是 AP 间耦合约束，不计入单 AP 边界，候选方案需继续用 `evaluate_candidate` 验证。

```python
from src.tools.sr import compute_feasible_ranges

result = compute_feasible_ranges(ap_states)
# {
#   "ranges": {
#     "ap1": {
#       "current_dbm": 20.0, "min_dbm": 1.0, "max_dbm": 9.0,
#       "feasible_individual_range": True,
#       "min_delta_db": -19.0, "max_delta_db": -11.0,
#       "lower_reasons": ["tx_power_min", "sta_rssi_min"],
#       "upper_reasons": ["cca_at_ap2"],
#       "sta_rssi_margin_at_min_db": 0.0,
#     }, ...
#   },
#   "sinr_coupled": True,
#   "candidate_hints": {
#     "minimal_necessary_drop": {"ap1": 9.0, ...},   # 接近上界，降幅最小
#     "conservative_mid_range": {"ap1": 5.0, ...},   # 区间中点，余量较大
#   },
#   "all_individual_ranges_feasible": True,
#   "notes": ["候选功率必须继续调用 evaluate_sr_candidate 验证 CCA/SINR/STA RSSI。", ...],
# }
```

---

### `recommend_tx_power(ap_states) → dict`

求解每个 AP 的连续最优功率。

```python
from src.tools.sr import recommend_tx_power

result = recommend_tx_power(ap_states)
# {
#   "ap1": {
#     "optimal_dbm": 6.59, "recommended_dbm": 6.59,
#     "current_dbm": 20.0, "delta_db": -13.41,
#     "active_constraints": ["upper_bound", "cca"],
#   },
#   "ap2": {"optimal_dbm": 6.69, "recommended_dbm": 6.69, "current_dbm": 14.0, "delta_db": -7.31, ...},
#   "ap3": {"optimal_dbm": 7.39, "recommended_dbm": 7.39, "current_dbm": 8.0,  "delta_db": -0.61, ...}
# }
```

`active_constraints` 列举在最优解处哪些约束紧绑定（`lower_bound` / `upper_bound` / `cca` / `sinr` / `sta_rssi`）。

---

### `validate(ap_states, proposed_powers) → (bool, list[str])`

验证提案功率是否满足所有约束。

```python
from src.tools.sr import validate

ok, errors = validate(ap_states, {"ap1": 6.0, "ap2": 6.0, "ap3": 6.0})
# (True, [])

ok, errors = validate(ap_states, {"ap1": 20.0, "ap2": 20.0, "ap3": 20.0})
# (False, ["AP1: CCA=-65.0 dBm ≥ 阈值 -82.0 dBm", ...])
```

---

### `evaluate_candidate(ap_states, proposed_powers) → dict`

评估单个候选 Co-SR 方案的合法性与量化评分，不替 agent 决策。

```python
from src.tools.sr import evaluate_candidate

result = evaluate_candidate(ap_states, {"ap1": 9.0, "ap2": 9.0, "ap3": 8.0})
# {
#   "valid": True,
#   "errors": [],
#   "proposed_powers": {"ap1": 9.0, "ap2": 9.0, "ap3": 8.0},
#   "score": {
#     "total_power_drop_db": 17.0,
#     "max_single_ap_drop_db": 11.0,
#     "sum_squared_power_change_db": 170.0,
#     "min_sta_rssi_margin_db": 4.0,
#     "max_cca_dbm": -83.5,
#     "min_sinr_db": 16.2,
#   },
#   "per_ap": {
#     "ap1": {"proposed_power_dbm": 9.0, "cca_max_dbm": -83.5, "cca_ok": True,
#             "sinr_db": 16.2, "sinr_ok": True, "sta_rssi_dbm": -71.0, "sta_rssi_ok": True,
#             "valid": True, "errors": []},
#     ...
#   },
# }
```

---

### `rank_candidates(ap_states, candidates, objective="balanced") → dict`

对多个候选方案按目标函数排序。

`objective` 可选值：

| 值 | 排序逻辑 |
|---|---|
| `balanced`（默认） | 先合法，再最小化平方调整量和最大单 AP 降幅 |
| `minimize_total_drop` | 先合法，再最小化总降功率 |
| `minimize_max_drop` | 先合法，再最小化最大单 AP 降幅 |
| `maximize_sta_margin` | 先合法，再最大化 STA RSSI 余量 |

```python
from src.tools.sr import rank_candidates

candidates = {
    "proposal_A": {"ap1": 9.0, "ap2": 9.0, "ap3": 8.0},
    "proposal_B": {"ap1": 6.0, "ap2": 6.0, "ap3": 6.0},
}
result = rank_candidates(ap_states, candidates, objective="balanced")
# {
#   "objective": "balanced",
#   "best": {"name": "proposal_A", "rank": 1, "valid": True, ...},
#   "ranked_candidates": [
#     {"name": "proposal_A", "rank": 1, "valid": True, "proposed_powers": {...}, "score": {...}, "errors": []},
#     {"name": "proposal_B", "rank": 2, "valid": True, ...},
#   ],
# }
```

`candidates` 也可传入列表格式（每项含 `name` 和 `proposed_powers` 键）。

---

### `compute_validation(ap_states, proposed_powers) → dict`

验算指定功率组合下各 AP 的三重约束，返回结构化 per-AP 详情，供 orchestrator 在投票阶段注入给投票方（避免 LLM 自行计算 delta 出错）。

```python
from src.tools.sr import compute_validation

details = compute_validation(ap_states, {"ap1": 6.0, "ap2": 6.0, "ap3": 6.0})
# {
#   "ap1": {
#     "proposed_power_dbm": 6.0,
#     "cca_max_dbm": -83.0, "cca_ok": True,
#     "cca_detail": {"ap2": {"received_dbm": -83.0, "ok": True}, ...},
#     "sinr_db": 22.8,      "sinr_ok": True,
#     "sta_rssi_dbm": -59.0,"sta_rssi_ok": True,
#     "valid": True, "errors": []
#   }, ...
# }
```

与 `validate()` 不同，此函数返回完整的每 AP 详情字典而非 `(bool, list)` 元组。

---

## orchestrator 中的调用位置

`src/orchestrator.py` 的 `_phase_propose()` 会向提案方开放分解后的 Co-SR 工具：
先获取最新状态，再分析干扰、计算可行区间、比较多个候选，并验证最终候选。

---

## 策略路由（orchestrator 中的触发逻辑）

| 条件 | 策略 |
|---|---|
| 任一 AP 的 neighbor_rssi ≥ -70 dBm | Co-SR |
| 任一 AP 的 channel_busy_ratio ≥ 0.60 或 tx_retries_ratio ≥ 0.15 | Co-EDCA |
| 两类条件同时满足 | Joint（同时调整 TX Power + EDCA） |

---

## 与 edca.py 的对比

| | Co-EDCA | Co-SR |
|---|---|---|
| 输入耦合 | 每 AP 独立 | AP 间耦合（一 AP 功率影响所有邻居 CCA） |
| 计算方式 | 直接映射（等级→参数） | 扫描功率空间（优化搜索） |
| 物理模型 | 无，纯阈值映射 | RSSI 线性传播模型 |
| 约束类型 | 单 AP 内部约束 | 跨 AP 联合约束（三重） |
