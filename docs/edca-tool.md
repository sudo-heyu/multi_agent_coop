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
| `critical` | 15 | 63 | 4 | 强制大退避，优先缓解拥塞 |

---

## 函数接口

### `classify_congestion(channel_busy_ratio, tx_retries_ratio) → str`

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
# → {"CWmin": 15, "CWmax": 63, "AIFSN": 4}
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

### `compute_all(ap_states) → dict`

主入口，对所有 AP 一次性完成分级、映射和验证。

**输入**：

```python
ap_states = {
    "ap1": {"channel_busy_ratio": 0.82, "tx_retries_ratio": 0.31, ...},
    "ap2": {"channel_busy_ratio": 0.55, "tx_retries_ratio": 0.12, ...},
    "ap3": {"channel_busy_ratio": 0.38, "tx_retries_ratio": 0.05, ...},
}
```

只需包含 `channel_busy_ratio` 和 `tx_retries_ratio`，其余字段忽略（与 `get_all_states()` 返回格式直接兼容）。

**输出**：

```python
{
    "ap1": {"congestion_level": "critical", "CWmin": 15, "CWmax": 63, "AIFSN": 4, "valid": True, "errors": []},
    "ap2": {"congestion_level": "medium",   "CWmin":  7, "CWmax": 31, "AIFSN": 3, "valid": True, "errors": []},
    "ap3": {"congestion_level": "low",      "CWmin":  7, "CWmax": 15, "AIFSN": 2, "valid": True, "errors": []},
}
```

---

## orchestrator 中的调用位置

`src/orchestrator.py` 的 `_phase_propose()` 在调用 agent 前运行工具：

```python
edca_result = edca_compute(ap_state)   # 计算基准推荐
# 将结果序列化后注入 agent 的 instruction，agent 在此基础上生成提案
```

控制台输出示例（提案阶段开始时打印）：

```
[Co-EDCA 工具] AP1=critical→CWmin=15,CWmax=63,AIFSN=4  AP2=medium→CWmin=7,CWmax=31,AIFSN=3  AP3=low→CWmin=7,CWmax=15,AIFSN=2
```

---

## 与 agent 的关系

工具输出作为**基准参考值**注入提案方的指令，agent 可在此基础上根据全局判断微调。
当前测试中 agent 的提案与工具推荐完全一致，说明工具的分级映射与 LLM 的直觉判断吻合。

validator-agent（第三步）也可调用此工具对提案进行独立核算，不依赖 LLM 自评。
