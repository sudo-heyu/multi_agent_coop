# 工具清单

底层函数由 `openclaw/mcp/multiap_mcp.py` 包装为 MCP 工具供 AP 调用。默认阶段编排直接运行 Python `structured_relay`；`run_fast_negotiation` 仅供兼容 coordinator 路径使用。

| MCP 工具 | 底层能力 |
|---|---|
| `get_latest_ap_states` | 状态服务器读取 + profile 过滤 |
| `analyze_sr_interference` | `analyze_interference` |
| `compute_sr_feasible_ranges` | `compute_feasible_ranges` |
| `select_sr_concurrent_groups` | 部分并发组选择 |
| `evaluate_sr_candidate` | `evaluate_candidate` / 并发组评估 |
| `rank_sr_candidates` | `rank_candidates` |
| `validate_edca_proposal` | EDCA 合法性、有效性和优先级检查 |
| `run_fast_negotiation` | coordinator 兼容入口 |

## Co-SR 工具（`src/tools/sr.py`）

| 函数 | 签名 | 返回 | 用途 |
|---|---|---|---|
| `classify_interference` | `(rssi_dbm: float)` | `str` | 判断单对 AP 间干扰等级：`strong` / `moderate` / `weak` |
| `compute_interference_matrix` | `(ap_states: dict)` | `dict` | 构建全网 AP 间干扰矩阵，每条链路含 RSSI 和等级 |
| `analyze_interference` | `(ap_states: dict)` | `dict` | 分析干扰关系：强/中链路列表、干扰源/受害 AP 排名、是否触发 Co-SR |
| `compute_feasible_ranges` | `(ap_states: dict)` | `dict` | 计算每 AP 的连续 TX Power 可行区间（CCA + STA RSSI 约束），附候选提示 |
| `recommend_tx_power` | `(ap_states: dict)` | `dict` | 连续优化求解满足三重约束的最小必要功率调整，返回 `optimal_dbm` / `delta_db` / `active_constraints` |
| `validate` | `(ap_states: dict, proposed_powers: dict)` | `(bool, list[str])` | 验证一组功率是否满足所有约束，返回 `(合法, 错误列表)` |
| `compute_validation` | `(ap_states: dict, proposed_powers: dict)` | `dict` | 同上，但返回结构化 per-AP 详情字典（含 CCA/SINR/STA RSSI 各字段） |
| `evaluate_candidate` | `(ap_states: dict, proposed_powers: dict)` | `dict` | 评估单个候选方案：合法性 + 6 项量化评分（总降功率、平方调整量等） |
| `rank_candidates` | `(ap_states: dict, candidates: object, objective: str)` | `dict` | 按目标函数对多候选排序；`objective` 支持 `balanced` / `minimize_total_drop` / `minimize_max_drop` / `maximize_sta_margin` |

---

## Co-EDCA 工具（`src/tools/edca.py`）

| 函数 | 签名 | 返回 | 用途 |
|---|---|---|---|
| `classify_congestion` | `(Data_rate_to_bandwidth_ratio: float, tx_retries_ratio: float)` | `str` | 判断单个 AP 的拥塞等级：`low` / `medium` / `high` / `critical` |
| `recommend_edca` | `(congestion_level: str)` | `dict` | 将拥塞等级映射为推荐 EDCA 参数（`CWmin` / `CWmax` / `AIFSN`） |
| `validate` | `(params: dict)` | `(bool, list[str])` | 验证 EDCA 参数是否在 IEEE 802.11 合法范围内，返回 `(合法, 错误列表)` |
| `evaluate_edca_effectiveness` | `(ap_states: dict, proposed_edca: dict)` | `dict` | 评估提案参数合理性：拥塞匹配度、碰撞概率估算、跨 AP AIFSN 公平性 |
