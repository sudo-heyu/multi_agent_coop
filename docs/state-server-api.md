# 全局状态接口文档

本文档面向香蕉派 AP 侧开发者，说明如何向 DGX Spark 上的状态服务器上报数据。

服务运行在 DGX Spark，地址：`http://<SPARK_IP>:5001`

---

## 接口一览

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/state` | AP 上报自身最新指标 |
| GET | `/state` | 获取全部三台 AP 的最新状态 |
| GET | `/state/<ap_id>` | 获取单台 AP 的最新状态 |
| GET | `/health` | 服务健康检查 |

---

## POST `/state` — 上报指标

香蕉派每隔 10 秒调用一次，上报自身当前采集到的全部指标。

### 请求

- Content-Type: `application/json`
- 每台香蕉派只上报自己的数据，`ap_id` 固定为该机器对应的编号

**字段说明**：

| 字段 | 类型 | 单位 | 说明 |
|---|---|---|---|
| `ap_id` | string | — | `"ap1"` / `"ap2"` / `"ap3"` |
| `timestamp` | string | ISO 8601 | 采集时刻，建议带时区（UTC）|
| `tx_power_dbm` | float | dBm | 当前发射功率 |
| `cwmin` | int | — | 竞争窗口下限 |
| `cwmax` | int | — | 竞争窗口上限 |
| `aifsn` | int | — | 仲裁帧间间隔数 |
| `Data_rate_to_bandwidth_ratio` | float | 0–1 | 信道利用率（data rate / bandwidth），原 `channel_busy_ratio` 改名 |
| `tx_retries_ratio` | float | 0–1 | 重传包占比 |
| `neighbor_rssi_dbm` | object | dBm | 邻居 AP 信号强度，key 为对方 ap_id |
| `sta_rssi_dbm` | float | dBm | 己方关联 STA 的信号强度 |
| `noise_floor_dbm` | float | dBm | 本底噪声 |
| `throughput_mbps_iperf` | float | Mbps | iperf 测试流吞吐量 |
| `throughput_mbps_user` | float | Mbps | 用户业务流吞吐量 |
| `ac_iperf` | string | — | iperf 流接入类别 AC（`BE`/`BK`/`VI`/`VO`） |
| `ac_user` | string | — | 用户流接入类别 AC（`BE`/`BK`/`VI`/`VO`） |
| `latency_ms` | float | ms | 端到端往返延迟 |
| `packet_loss_pct` | float | % | 丢包率（0–100） |
| `source` | string | — | 可选，默认视为 `ap`；真实部署不得使用 `mock` / `generated` / `synthetic` 等生成源 |

**请求示例（ap1）**：

```bash
curl -X POST http://<SPARK_IP>:5001/state \
  -H "Content-Type: application/json" \
  -d '{
    "ap_id": "ap1",
    "timestamp": "2026-05-19T07:15:00.000000+00:00",
    "tx_power_dbm": 16.0,
    "cwmin": 3,
    "cwmax": 7,
    "aifsn": 1,
    "Data_rate_to_bandwidth_ratio": 0.82,
    "tx_retries_ratio": 0.31,
    "neighbor_rssi_dbm": {"ap2": -68.0, "ap3": -75.0},
    "sta_rssi_dbm": -55.0,
    "noise_floor_dbm": -92.0,
    "throughput_mbps_iperf": 18.4,
    "throughput_mbps_user": 11.0,
    "ac_iperf": "BK",
    "ac_user": "VO",
    "latency_ms": 312.0,
    "packet_loss_pct": 1.2,
    "source": "ap"
  }'
```

### 响应

**成功（200）**：
```json
{"ok": true, "ap_id": "ap1"}
```

**失败（400）**：
```json
{"error": "missing fields: ['cwmin', 'tx_power_dbm']"}
```

---

## GET `/state` — 获取全局状态

返回三台 AP 的最新数据及数据新鲜度信息。

**请求示例**：

```bash
curl http://<SPARK_IP>:5001/state
```

**响应示例**：

```json
{
  "ap1": {
    "data": {
      "tx_power_dbm": 16.0,
      "cwmin": 3, "cwmax": 7, "aifsn": 1,
      "Data_rate_to_bandwidth_ratio": 0.82,
      "tx_retries_ratio": 0.31,
      "neighbor_rssi_dbm": {"ap2": -68.0, "ap3": -75.0},
      "sta_rssi_dbm": -55.0,
      "noise_floor_dbm": -92.0,
      "throughput_mbps_iperf": 18.4,
      "throughput_mbps_user": 11.0,
      "ac_iperf": "BK",
      "ac_user": "VO",
      "latency_ms": 312.0,
      "packet_loss_pct": 1.2
    },
    "timestamp": "2026-05-19T07:15:00+00:00",
    "age_seconds": 3.2,
    "stale": false
  },
  "ap2": { ... },
  "ap3": { ... }
}
```

**字段说明**：

| 字段 | 说明 |
|---|---|
| `data` | 与 POST 上报的 body 相同（不含 `ap_id` 和 `timestamp`） |
| `timestamp` | 该 AP 最近一次上报的时刻 |
| `age_seconds` | 距上次上报经过的秒数 |
| `stale` | `true` 表示超过 60 秒未上报，数据不可信 |

若某台 AP 从未上报过，其 `data` 为 `null`，`stale` 为 `true`。

---

## GET `/state/<ap_id>` — 获取单台 AP 状态

```bash
curl http://<SPARK_IP>:5001/state/ap1
```

响应结构与 GET `/state` 中单个 AP 的条目相同。

---

## GET `/health` — 健康检查

```bash
curl http://<SPARK_IP>:5001/health
```

```json
{
  "ok": true,
  "reported": {
    "ap1": true,
    "ap2": false,
    "ap3": true
  }
}
```

`reported` 中 `false` 表示该 AP 从未上报过数据（服务启动后尚未收到该 AP 的请求）。

---

## 香蕉派侧接入要点

1. **每台香蕉派只上报自己的数据**，`ap_id` 硬编码为该机器对应编号
2. **上报周期建议 10 秒**，超过 60 秒未上报服务器会将该 AP 标记为 `stale`
3. **`neighbor_rssi_dbm`** 填写本机扫描到的邻居 BSS 信号强度，key 使用对方的 `ap_id`
4. **时间戳建议使用 UTC**，格式 `2026-05-19T07:15:00.000000+00:00`
5. 除 `source` 外所有字段均为必填，缺少任一字段服务器返回 400
6. 真实部署默认拒收 `source=mock/generated/synthetic/simulated/simulation/random`，禁止把生成数据作为真实 QoS 观测上报

---

## 启动服务器

在 DGX Spark 上：

```bash
python state_server/server.py
# 服务监听 0.0.0.0:5001
# 默认真实上报模式，拒收生成数据源
```

## ns-3 联调（无香蕉派时）

```bash
# 终端 1：启动服务器（只接受 source=ns3 或 source=ap）
python state_server/server.py

# 终端 2：转发 ns-3 生成的 JSONL 遥测
python state_server/ns3_bridge.py --input ns3-output.jsonl --follow

# 终端 3：触发协商（从服务器拉取 ns-3 状态）
python run_openclaw.py --data-source ns3
```
