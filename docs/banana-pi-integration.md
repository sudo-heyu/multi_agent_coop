# 香蕉派接入手册

---

## 一、状态上报接口

### POST `http://<SPARK_IP>:5001/state`

每 10 秒调用一次，上报本机全部指标。

**请求体**：

```json
{
  "ap_id": "ap1",
  "timestamp": "2026-05-21T07:00:00.000000+00:00",
  "service_name": "background_download",
  "business_type": "后台下载",
  "traffic_priority": "low",
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
  "latency_ms": 312.0,
  "packet_loss_pct": 1.2,
  "source": "ap"
}
```

服务器硬性必填字段以 `state_server/server.py` 的 `REQUIRED_FIELDS` 为准；业务字段缺省时由 profile 使用默认值，但真实部署应明确上报 `service_name`、`business_type` 和 `traffic_priority`。`ap_id` 固定为本机编号。真实部署默认拒收 `source=mock/generated/synthetic/simulated/simulation/random`。

**响应**：

```json
{"ok": true, "ap_id": "ap1"}          // 200 成功
{"error": "missing fields: [...]"}     // 400 字段缺失
```

---

## 二、决策执行接口

### POST `http://<AP_IP>:5002/apply`

DGX 协商完成后主动调用，香蕉派收到后立即执行参数变更。

**请求体**（字段随 `strategy` 变化）：

```json
{
  "ap_id": "ap1",
  "strategy": "co_sr",
  "session_id": "session_20260521_082120_1a576004",
  "params": {
    "tx_power_dbm": 10.5
  }
}
```

| `strategy` | `params` 包含的字段 |
|---|---|
| `co_sr` | `tx_power_dbm` |
| `co_edca` | `CWmin`、`CWmax`、`AIFSN` |
| `joint` | `tx_power_dbm`、`CWmin`、`CWmax`、`AIFSN` |

**响应**：

```json
// 200 成功
{
  "ok": true,
  "ap_id": "ap1",
  "applied_at": "2026-05-21T07:02:13+00:00",
  "details": {
    "tx_power": {"ok": true, "value_dbm": 10.5},
    "edca":     {"ok": true, "CWmin": 15, "CWmax": 63, "AIFSN": 3}
  }
}

// 500 执行失败
{
  "ok": false,
  "details": {
    "tx_power": {"ok": false, "output": "Operation not permitted"}
  }
}

// 400 请求错误
{"ok": false, "error": "ap_id mismatch: got 'ap2', expected 'ap1'"}
```

### GET `http://<AP_IP>:5002/status`

查询最近一次执行结果，响应格式与 `/apply` 成功响应相同。若尚未收到任何决策，返回 `{"ap_id": "ap1", "status": "idle"}`。

---

## 三、香蕉派操作部分

### 3.1 启动两个进程

```bash
# 进程 1：状态上报（后台）
python state_server/reporter.py \
  --ap-id ap1 \
  --server http://192.168.1.100:5001 \
  --interval 10 \
  --iface wlan0 &

# 进程 2：决策执行服务（前台）
python state_server/executor.py \
  --ap-id ap1 \
  --iface wlan0 \
  --port 5002
```

三台机器分别将 `--ap-id` 改为 `ap1` / `ap2` / `ap3`。

### 3.2 各指标采集命令

| 字段 | 采集命令 |
|---|---|
| `tx_power_dbm` | `iw dev wlan0 info \| grep txpower` |
| `Data_rate_to_bandwidth_ratio` | `iw dev wlan0 survey dump`，取 `busy_time / active_time` |
| `tx_retries_ratio` | `iw dev wlan0 station dump`，取 `tx_retries / tx_packets` |
| `neighbor_rssi_dbm` | `iw dev wlan0 scan`，按 BSSID→ap_id 映射提取 signal |
| `sta_rssi_dbm` | `iw dev wlan0 station dump`，取关联 STA 的 signal（多 STA 取最强） |
| `noise_floor_dbm` | `iw dev wlan0 survey dump`，取 `channel noise` |
| `cwmin/cwmax/aifsn` | `hostapd_cli -i wlan0 get_edca_params 0`（队列 0 = BE） |
| `throughput_mbps_iperf` | `iperf3 -c <SPARK_IP> -t 5 -J`，取 `sum_received.bits_per_second / 1e6` |
| `latency_ms` | `ping -c 5 <SPARK_IP>`，取 avg RTT |
| `packet_loss_pct` | `ping -c 20 <SPARK_IP>`，取丢包百分比 |

`neighbor_rssi_dbm` 需要维护一张 BSSID 到 ap_id 的映射文件，例如：

```json
{"aa:bb:cc:dd:ee:f2": "ap2", "aa:bb:cc:dd:ee:f3": "ap3"}
```

### 3.3 决策参数应用命令

**Co-SR — 设置发射功率**（1 dBm = 100 mBm）：

```bash
# tx_power_dbm = 10.5 → 1050 mBm
iw dev wlan0 set txpower fixed 1050

# 验证
iw dev wlan0 info | grep txpower
```

**Co-EDCA — 设置 EDCA 参数**（队列 0 = Best Effort）：

```bash
# hostapd_cli set_edca_params <queue> <aifs> <cwmin> <cwmax> <txop>
# 示例：AIFSN=3, CWmin=15, CWmax=63
hostapd_cli -i wlan0 set_edca_params 0 3 15 63 0

# 验证
hostapd_cli -i wlan0 get_edca_params 0
```

**joint** — 两条命令都执行，顺序为先功率后 EDCA。

### 3.4 权限与常见错误

`iw set txpower` 和 `hostapd_cli` 需要 root 权限。推荐配置 sudoers 免密：

```
# /etc/sudoers.d/ap-executor
<user> ALL=(ALL) NOPASSWD: /usr/sbin/iw, /usr/sbin/hostapd_cli
```

| 错误 | 原因 | 处理 |
|---|---|---|
| `Operation not permitted` | 非 root / regulatory 限制 | 加 `sudo`；`iw reg set CN` 解除功率上限 |
| `hostapd_cli: Connection refused` | hostapd 未运行或接口名错 | `systemctl start hostapd`；`iw dev` 确认接口名 |
| DGX 推送超时 | 防火墙拦截 5002 端口 | `sudo ufw allow from 192.168.1.100 to any port 5002` |

---

## 四、DGX 侧启动与真实协商

默认入口要求常驻 state server、OpenClaw gateway 和 Dashboard 在线：

```bash
MULTIAP_STATE_MODE=real bash openclaw/serve.sh restart
bash openclaw/serve.sh status

.venv/bin/python run_openclaw.py \
  --mode real \
  --scene joint \
  --server http://localhost:5001 \
  --ap-endpoints ap1=192.168.1.1:5002,ap2=192.168.1.2:5002,ap3=192.168.1.3:5002
```

执行端点不会自动从 `ap_endpoints.json` 读取，必须显式传入 `--ap-endpoints` 或 `--ap-config`。`--mode real` 保证不创建 mock feeder，检查 state server 已拒收生成源，等待三台 AP 的 `source=ap` 新鲜数据，并要求端点恰好覆盖 ap1/ap2/ap3。

## 五、本地联调（无真实硬件）

```bash
# 终端 1：常驻服务（state server 已带 --allow-mock）
bash openclaw/serve.sh start

# 终端 2
python state_server/reporter.py --mock --all --server http://localhost:5001

# 终端 3（三台 AP 用不同端口模拟）
python state_server/executor.py --ap-id ap1 --mock --port 5002 &
python state_server/executor.py --ap-id ap2 --mock --port 5003 &
python state_server/executor.py --ap-id ap3 --mock --port 5004 &

# 终端 4（使用内置 feeder 时可省略终端 2）
.venv/bin/python run_openclaw.py --scene joint \
  --ap-endpoints ap1=localhost:5002,ap2=localhost:5003,ap3=localhost:5004
```

执行后查询结果：

```bash
curl http://localhost:5002/status   # ap1 执行结果
curl http://localhost:5003/status   # ap2
curl http://localhost:5004/status   # ap3
```
