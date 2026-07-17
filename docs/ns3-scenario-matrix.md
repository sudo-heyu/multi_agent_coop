# ns-3 场景参数矩阵

当前 fulltool 实验只保留 `co_sr` 和 `co_edca` 两类策略。为了让每个已有 ns-3 场景组合的预期效果清晰可见，实验启动参数按下表固定。

| topology | businessProfile | 预期策略 | 调整参数 | 预期效果 |
|---|---|---|---|---|
| line | live_bulk | co_edca | 默认 | high/medium/low 优先级不同，SR 不强触发，EDCA 应改变 BE 容量份额 |
| line | mixed_qoe | co_edca | 默认 | high/low 优先级不同，SR 不强触发 |
| line | deadline_backup | co_edca | 默认 | high/medium/low 优先级不同，SR 不强触发 |
| line | uniform | noop | 默认 | 全部 medium 且 SR 不强触发 |
| triangle | live_bulk | co_sr | `--spacing=17` | 强 OBSS 干扰；降功率后邻居 RSSI 可低于 CCA 边界 |
| triangle | mixed_qoe | co_sr | `--spacing=17` | 同时有优先级差异和强干扰，当前规则先处理 SR |
| triangle | deadline_backup | co_sr | `--spacing=17` | 同时有优先级差异和强干扰，当前规则先处理 SR |
| triangle | uniform | co_sr | `--spacing=17` | 无 EDCA 需求，但强干扰足够触发 SR |
| asym | live_bulk | co_edca | 默认 | high/medium/low 优先级不同，SR 不强触发 |
| asym | mixed_qoe | co_edca | 默认 | high/low 优先级不同，SR 不强触发 |
| asym | deadline_backup | co_edca | 默认 | high/medium/low 优先级不同，SR 不强触发 |
| asym | uniform | noop | 默认 | 全部 medium 且 SR 不强触发 |

生成命令：

```bash
.venv/bin/python state_server/ns3_scenario_matrix.py
```

单个组合：

```bash
.venv/bin/python state_server/ns3_scenario_matrix.py --scenario triangle --business-profile uniform
```

关键调参依据：

- `triangle` 默认 `15m` 时，SR 工具已把 TX power 降到 `1 dBm`，但邻居 RSSI 仍约 `-80.96 dBm`，没有明显跨过默认 CCA `-82 dBm`。
- `triangle --spacing=17` 时，baseline 邻居 RSSI 仍约 `-67.59 dBm`，足够触发 SR；降到 `1 dBm` 后邻居 RSSI 约 `-82.59 dBm`，三 AP 的 BE 吞吐明显提升。
- 当前 ns-3 的 Co-EDCA 主要改变 AC_BE 容量份额；user 流是 AC_VI，因此 EDCA 场景的直接效果应看 `throughput_mbps_iperf` 和 BE 份额，而不是只看 user 延迟。
- ns-3 `TELEMETRY` 与 stdin `APPLY` 都使用实际 CW 值（如 `CWmin=15,CWmax=1023`）。真实 AP/hostapd reporter 才上报 ECW 指数，profile 层只对 `source=ap` 做指数到实际 CW 的解码。
