# AP 状态字段参考

各字段由香蕉派 AP 定期采集后 POST 到状态服务器，以下说明每个字段的含义、来源和在协商中的作用。

## 业务语义

| 字段 | 类型 | 含义 |
|---|---|---|
| `service_name` | string | 机器可读业务名；缺省为“未声明业务” |
| `business_type` | string | 面向展示与协商的业务标签；缺省为“未声明业务类型” |
| `traffic_priority` | string | `high` / `medium` / `low`；用于 EDCA 优先级单调性验算 |

## 可调参数（由协商决策修改）

| 字段 | 类型 | 单位 | 含义 | 协商作用 |
|---|---|---|---|---|
| `tx_power_dbm` | float | dBm | 发射功率 | Co-SR：控制覆盖半径与对邻居 BSS 的干扰强度；功率越高干扰越强，空间复用机会越少 |
| `cwmin` | int | - | 竞争窗口下限 | Co-EDCA：值越小初始退避越短，竞争越激烈；AP 间协商防止集体取小值加剧碰撞 |
| `cwmax` | int | - | 竞争窗口上限 | Co-EDCA：决定重传时退避时间的增长上界；与 cwmin 共同决定退避分布 |
| `aifsn` | int | - | 仲裁帧间间隔数 | Co-EDCA：等待信道空闲的最小时隙数；值越小优先级越高；协商统一策略防止某 AP 长期独占信道 |

## 采集指标（只读，驱动协商触发与效果验证）

| 字段 | 类型 | 单位 | 含义 | 协商作用 |
|---|---|---|---|---|
| `Data_rate_to_bandwidth_ratio` | float | 0-1 | 信道繁忙时间占活跃时间比例 | 协商触发的核心判据；Co-EDCA 分级的主要输入 |
| `tx_retries_ratio` | float | 0-1 | 重传包占总发包比例 | 协商触发依据之一；Co-EDCA 调整效果的直接验证指标，调整后此值应下降 |
| `neighbor_rssi_dbm` | object | dBm | 本机扫描到的邻居 AP 信号强度，key 为对方 ap_id | Co-SR 的感知指标，用于判断干扰情况；值越高（越接近 0）说明干扰越强 |
| `sta_rssi_dbm` | float | dBm | 己方关联 STA 的信号强度 | 降功率的安全下界，调整后需保证此值 > -75 dBm，否则 STA 可能断连 |
| `noise_floor_dbm` | float | dBm | 信道本底噪声 | 用于估算 SINR；`iw survey dump` 中 noise 字段直接可读 |

## 业务质量指标（只读，协商效果对比依据）

| 字段 | 类型 | 单位 | 含义 | 协商作用 |
|---|---|---|---|---|
| `throughput_mbps_iperf` | float | Mbps | STA 实际接收速率 | 协商效果最直接的体现；协商后应上升 |
| `throughput_mbps_user` | float | Mbps | 用户业务流吞吐量 | 与 iperf 流分开观察业务收益 |
| `ac_iperf` / `ac_user` | string | — | 两类流量使用的 EDCA AC | 展示流量分类与参数效果 |
| `latency_ms` | float | ms | 端到端往返时延 | 协商触发判据之一；协商后应下降 |
| `packet_loss_pct` | float | % | 数据包丢失比例 | 反映信道质量恶化程度；协商后应下降 |

## 协商触发阈值（参考）

| 指标 | 触发阈值 | 说明 |
|---|---|---|
| `Data_rate_to_bandwidth_ratio` | >= 0.60 | 信道重度拥塞 |
| `tx_retries_ratio` | >= 0.15 | 重传率过高 |
| `latency_ms` | >= 200 ms | 延迟超出可接受范围 |
| `packet_loss_pct` | >= 1.0 % | 丢包率异常 |

这些是参考阈值。现行触发逻辑由 `openclaw/mcp/orchestration.py` 的 `determine_strategy` 实现，并结合邻居 RSSI、业务优先级和 EDCA 状态选择 `co_sr`、`co_edca` 或 `noop`。

## 字段采集命令参考（香蕉派）

| 字段 | 采集命令 |
|---|---|
| `tx_power_dbm` | `iw dev wlan0 info \| grep txpower` |
| `Data_rate_to_bandwidth_ratio` | `iw dev wlan0 survey dump`（busy_time / active_time） |
| `tx_retries_ratio` | `iw dev wlan0 station dump`（tx_retries / tx_packets） |
| `neighbor_rssi_dbm` | `iw dev wlan0 scan`（邻居 BSS 的 signal 字段） |
| `sta_rssi_dbm` | `iw dev wlan0 station dump`（关联 STA 的 signal） |
| `noise_floor_dbm` | `iw dev wlan0 survey dump`（noise 字段） |
| `throughput_mbps_iperf` | iperf3 客户端测量 |
| `latency_ms` | `ping -c 10 <网关> \| tail -1 \| awk '{print $4}' \| cut -d/ -f2` |
| `packet_loss_pct` | `ping -c 20 <网关> \| grep loss \| awk '{print $6}'` |
