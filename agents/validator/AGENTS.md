# Validator 行为规范

Validator 是一个**确定性 Python 模块**，不通过 LLM 运行。

## 触发时机

协商成功（所有 AP 投票通过）→ 提案方输出最终 JSON 决策
→ orchestrator 调用 `validator.validate_decision()` → 写入 `validation_result` 日志事件

## 验证项目

### Co-SR 验证
1. 决策 JSON 包含每个 AP 的 `tx_power_dbm`
2. `tx_power_dbm` ∈ [1, 23] dBm
3. 物理约束（调用 `sr.validate()`）：
   - CCA < -82 dBm（邻居 AP 不触发 CCA）
   - SINR ≥ 15 dB（STA 链路质量）
   - STA RSSI ≥ -75 dBm（关联安全下界）

### Co-EDCA 验证
1. 决策 JSON 包含每个 AP 的 `CWmin`、`CWmax`、`AIFSN`
2. `CWmin` ∈ [3, 1023]
3. `CWmax` ∈ [7, 1023]
4. `AIFSN` ∈ [1, 15]
5. `CWmax > CWmin`

### 联合验证
同时执行 Co-SR 和 Co-EDCA 的全部验证项。

## 输出格式

`validate_decision()` 返回 dict，由 orchestrator 写入 JSONL：

```json
{
  "approved": true,
  "strategy": "co_sr",
  "parse_ok": true,
  "per_ap": {
    "ap1": {
      "proposed_params": {"tx_power_dbm": 10.0},
      "checks": [{"check": "Co-SR", "ok": true, "errors": []}],
      "valid": true,
      "errors": []
    }
  },
  "global_errors": [],
  "summary": "验证通过（策略=co_sr，所有 AP 参数合规）"
}
```

## 重要说明

- Validator **不阻断**协商执行：即使验证失败，日志中会记录 `approved=false`，但协调者仍可选择执行或回退。
- 验证失败时 `global_errors` 列出所有错误，`summary` 给出可读摘要。
- `parse_ok=false` 表示 LLM 未能输出合法 JSON，是最严重的失败类型。
