# 系统介绍与现行设计

## 目标

本项目让三台 Wi-Fi AP 基于实时状态进行多轮协商，并在确定性约束通过后调整：

- Co-SR：`tx_power_dbm`
- Co-EDCA：`CWmin`、`CWmax`、`AIFSN`
- joint：同一提案同时包含功率和 EDCA 调整

系统同时记录吞吐、延迟、丢包、信道利用率和重传率，用于展示调整前后的变化。

## 部署拓扑

```text
                          DGX Spark
       ┌────────────────────────────────────────────┐
       │ run_openclaw.py + structured_relay         │
       │ OpenClaw gateway + AP agents + MCP tools   │
       │ state server + Dashboard + Validator       │
       └───────────────┬────────────────────────────┘
                       │ Ethernet / management LAN
             ┌─────────┼─────────┐
             │         │         │
        Banana Pi 1 Banana Pi 2 Banana Pi 3
        reporter +  reporter +  reporter +
        executor     executor     executor
             │         │         │
            STA1      STA2      STA3
```

三台香蕉派周期上报本机状态到 DGX。协商成功后，DGX 向显式配置的 executor `/apply` 端点推送决策。

## 软件架构

- OpenClaw 托管 `ap1 / ap2 / ap3` 三个 agent，并为它们提供 MCP 工具。
- `run_openclaw.py` 是默认入口，直接调用 Python `structured_relay`；阶段顺序不由 LLM 决定。
- `coordinator` agent 仅保留为 `--use-coordinator` 兼容路径。
- Validator 是 `src/validator.py` 中的确定性代码，不是单独的 LLM agent。
- Dashboard 使用 Flask + SSE，不使用 React。
- state server、gateway、Dashboard 和 plot 由 `openclaw/serve.sh` 常驻管理。

详细实现见 [Agent 实现说明](agent-implementation.md)。

## 协商流程

1. 广播：三台 AP 并发生成自身状态广播，按 ap1、ap2、ap3 顺序展示。
2. 触发判断：确定性逻辑根据最新状态给出 `co_sr`、`co_edca`、`joint` 或 `noop` 提示。
3. 提案：首轮由 ap1 发起，AP 自主调用状态和计算工具后输出参数 JSON。
4. 投票：其他 AP 验算后同意、弃权或反对；反对者提交反提案并接管。
5. 决策：全票通过后直接采用已通过提案，Validator 做最终验收。
6. 执行：仅在显式配置 `--ap-endpoints` 或 `--ap-config` 时推送到真实 AP。

## 场景

| 场景 | CLI | 状态特征 | 允许的结果 |
|---|---|---|---|
| Co-SR | `--scene sr` | 邻居 RSSI 显示强/中干扰 | `co_sr` |
| Co-EDCA | `--scene edca` | 业务优先级和拥塞存在差异 | `co_edca` |
| 联合 | `--scene joint` | 同时存在干扰和 EDCA 差异 | `joint`，或基于实时证据先处理主导问题 |

代码当前没有第四个“动态 AP1 业务骤升”内置场景；动态变化可由真实 reporter 或外部状态源驱动。

## 状态与参数

| 类别 | 字段 |
|---|---|
| 业务语义 | `service_name`、`business_type`、`traffic_priority` |
| 可调参数 | `tx_power_dbm`、`cwmin`、`cwmax`、`aifsn` |
| 无线观测 | `Data_rate_to_bandwidth_ratio`、`tx_retries_ratio`、`neighbor_rssi_dbm`、`sta_rssi_dbm`、`noise_floor_dbm` |
| QoS | `throughput_mbps_iperf`、`throughput_mbps_user`、`latency_ms`、`packet_loss_pct`、`ac_iperf`、`ac_user` |

完整定义见 [状态字段参考](state-metrics.md) 和 [状态接口](state-server-api.md)。

## 运行

```bash
MULTIAP_PY="$PWD/.venv/bin/python" bash openclaw/setup.sh  # 首次
bash openclaw/serve.sh start
.venv/bin/python run_openclaw.py --scene joint
```

真实 AP 使用 `--mode real`，并显式提供三个 executor 端点。该模式完全禁用 feeder、要求 state server 拒收生成数据，并等待三台 reporter 状态就绪。具体见 [香蕉派接入手册](banana-pi-integration.md)。
