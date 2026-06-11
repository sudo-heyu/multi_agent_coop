# Co-EDCA 计算工具

模块路径：`src/tools/edca.py`

---

## 作用

根据各 AP 的实测信道指标（信道占用率 + 重传率）判断拥塞等级，
映射到推荐的 EDCA 参数组合（CWmin / CWmax / AIFSN），并验证参数合法性。

orchestrator 在第二阶段（提案）自动调用此工具，将结果注入提案方的指令，
作为 LLM 生成参数的物理基准。

---

## 拥塞分级规则

| 等级 | 触发条件 | 含义 |
|---|---|---|
| `low` | busy < 0.40 **且** retries < 0.08 | 信道空闲 |
| `medium` | busy < 0.60 **且** retries < 0.15 | 轻度拥塞 |
| `high` | busy ≥ 0.60 **或** retries ≥ 0.15 | 重度拥塞 |
| `critical` | busy ≥ 0.75 **且** retries ≥ 0.25 | 严重拥塞 |

判断按 critical → high → medium → low 顺序，首个满足条件的等级生效。

---

## 参数映射表

| 等级 | CWmin | CWmax | AIFSN | 策略 |
|---|---|---|---|---|
| `low` | 7 | 15 | 2 | 标准竞争，信道利用率高 |
| `medium` | 7 | 31 | 3 | 轻度退避，减少碰撞 |
| `high` | 15 | 63 | 3 | 中度退避，显著降低竞争激烈度 |
| `critical` | 15 | 127 | 4 | 最大退避窗口，强制缓解严重拥塞 |

> `high` 与 `critical` 现在有明确区分：`critical` 的 CWmax 翻倍（63→127），
> 在严重拥塞下能更大幅度延长退避时间、降低碰撞概率。

---

## 函数接口

### `classify_congestion(Data_rate_to_bandwidth_ratio, tx_retries_ratio) → str`

判断单个 AP 的拥塞等级。

```python
from src.tools.edca import classify_congestion

level = classify_congestion(0.82, 0.31)  # → "critical"
level = classify_congestion(0.55, 0.12)  # → "medium"
level = classify_congestion(0.38, 0.05)  # → "low"
```

---

### `recommend_edca(congestion_level) → dict`

将拥塞等级映射为推荐参数。

```python
from src.tools.edca import recommend_edca

params = recommend_edca("critical")
# → {"CWmin": 15, "CWmax": 127, "AIFSN": 4}
```

---

### `validate(params) → (bool, list[str])`

验证参数是否满足 IEEE 802.11 约束，返回 `(合法, 错误列表)`。

合法范围：CWmin ∈ [3, 1023]，CWmax ∈ [7, 1023]，AIFSN ∈ [1, 15]，且 CWmax > CWmin。

```python
from src.tools.edca import validate

ok, errors = validate({"CWmin": 15, "CWmax": 63, "AIFSN": 4})
# → (True, [])

ok, errors = validate({"CWmin": 63, "CWmax": 15, "AIFSN": 4})
# → (False, ["CWmax=15 必须大于 CWmin=63"])
```

---

### `evaluate_edca_effectiveness(ap_states, proposed_edca) → dict`

评估提案 EDCA 参数的合理性，供 `validate_edca_proposal` 工具在投票阶段调用。
返回三类信息：**拥塞匹配度**、**碰撞概率估算**、**跨 AP 公平性**。

```python
from src.tools.edca import evaluate_edca_effectiveness

ap_states    = { "ap1": {"Data_rate_to_bandwidth_ratio": 0.82, "tx_retries_ratio": 0.31, ...}, ... }
proposed_edca = { "ap1": {"CWmin": 7, "CWmax": 15, "AIFSN": 2}, ... }  # 太激进

result = evaluate_edca_effectiveness(ap_states, proposed_edca)
# result["per_ap"]["ap1"]["warnings"]
# → ["CWmin=7 比 critical 级推荐值 15 更激进，当前重传率 31%，..."]

# result["fairness"]["warnings"]
# → ["AIFSN 差值 3：..."]  # 当 max-min AIFSN ≥ 3 时触发

# result["all_ok"] → False（有 warning 时）
```

**per_ap 字段说明**：

| 字段 | 含义 |
|------|------|
| `recommended_level` | 工具根据 busy/retry 判断的推荐拥塞等级 |
| `cwmin_delta_vs_rec` | 提案 CWmin 与推荐值之差（正=更保守，负=更激进） |
| `aifsn_delta_vs_rec` | 提案 AIFSN 与推荐值之差 |
| `estimated_p_collision` | 简化碰撞概率估算（假设每 AP 2 个 STA） |
| `expected_backoff_us` | 含碰撞放大的期望退避时间（μs） |
| `warnings` | 合理性警告列表；空列表表示无异常 |

**fairness 字段说明**：

| 字段 | 含义 |
|------|------|
| `aifsn_spread` | 提案中最大与最小 AIFSN 之差 |
| `warnings` | 差值 ≥ 3 时触发，说明低 AIFSN AP 将系统性抢占信道 |

---

## orchestrator 中的调用位置

`src/orchestrator.py` 的 `_phase_propose()` 会让提案方先获取最新状态，再自行提出 EDCA 候选，并调用 `validate_edca_proposal` 验证最终候选。

validator-agent（第三步）也可调用此工具对提案进行独立核算，不依赖 LLM 自评。
