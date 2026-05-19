# Co-SR 计算工具

模块路径：`src/tools/sr.py`

---

## 作用

根据 AP 间 RSSI 感知干扰强度，扫描可行发射功率范围，
给出满足三重约束的推荐 TX Power，供 orchestrator 在第二阶段（提案）注入 LLM 指令。

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

## 扫描策略

从当前最高功率向下逐 1 dBm 扫描，找**满足所有约束的最高统一功率**。
选择最高可行功率而非最低，是为了在满足干扰约束的前提下保留最大覆盖能力。

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

### `scan_feasible_powers(ap_states) → dict`

扫描统一功率可行范围。

```python
from src.tools.sr import scan_feasible_powers

result = scan_feasible_powers(ap_states)
# {
#   "feasible": True,
#   "recommended_uniform_dbm": 6.0,
#   "current_max_power_dbm": 20.0,
#   "binding_constraint": "cca",   # 首次违约的约束类型
#   "scan_log": [{"power_dbm": 20, "ok": False, "violations": [...]}, ...]
# }
```

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

### `compute_all(ap_states) → dict`

主入口，一次性完成感知、扫描、推荐与验证。

**输入**（与 `get_all_states()` / mock 格式兼容）：

```python
ap_states = {
    "ap1": {
        "tx_power_dbm": 20.0,
        "neighbor_rssi_dbm": {"ap2": -65.0, "ap3": -72.0},
        "sta_rssi_dbm": -52.0,
        "noise_floor_dbm": -92.0,
        ...
    },
    ...
}
```

**输出**：

```python
{
    "interference_matrix": {
        "ap1->ap2": {"rssi_dbm": -65.0, "level": "strong"},
        ...
    },
    "feasible": True,
    "recommended_uniform_dbm": 6.0,
    "binding_constraint": "cca",
    "recommendations": {
        "ap1": {"recommended_dbm": 6.0, "current_dbm": 20.0, "delta_db": -14.0},
        "ap2": {"recommended_dbm": 6.0, "current_dbm": 20.0, "delta_db": -14.0},
        "ap3": {"recommended_dbm": 6.0, "current_dbm": 20.0, "delta_db": -14.0},
    },
    "validation": {
        "ap1": {
            "proposed_power_dbm": 6.0,
            "cca_max_dbm": -83.0, "cca_ok": True,
            "sinr_db": 24.5,      "sinr_ok": True,
            "sta_rssi_dbm": -66.0,"sta_rssi_ok": True,
            "valid": True, "errors": []
        },
        ...
    },
}
```

---

## orchestrator 中的调用位置

`src/orchestrator.py` 的 `_phase_propose()` 根据策略路由调用：

```python
if strategy in ("co_sr", "joint"):
    sr_result = sr_compute(ap_state)
    # 将结果序列化后注入提案方的 instruction
```

控制台输出示例（提案阶段开始时打印）：

```
[Co-SR 工具] AP1:当前20.0dBm→推荐6.0dBm  AP2:当前20.0dBm→推荐6.0dBm  AP3:当前20.0dBm→推荐6.0dBm
  干扰矩阵: ap1->ap2=-65.0dBm(strong)  ap1->ap3=-72.0dBm(moderate)  ...
```

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
