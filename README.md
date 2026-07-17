# Multi-AP 协商系统（纯 OpenClaw 架构）

多台 Wi-Fi AP 通过 LLM Agent 自主协商，协调发射功率（Co-SR）和 MAC 退避参数（Co-EDCA），提升整体网络性能。

**架构**：托管层与编排入口均由 **OpenClaw** 运行——`coordinator / ap1 / ap2 / ap3` 是各自独立 workspace/session 的 OpenClaw agent；确定性计算/验算/状态/下发逻辑以 **MCP 工具**暴露给 agent 调用。

**拓扑**：DGX Spark（运行 OpenClaw + 模型 provider + 状态服务器）+ 3 台香蕉派 AP。

---

## 运行指令

> 全部用项目 `.venv`（Python 3.11）；系统 python3 是 3.9、缺 `mcp` 包会失败。

**0. 依赖（一次性）**
```bash
pip install -r requirements.txt          # flask requests python-dotenv matplotlib mcp
npm install -g openclaw                   # OpenClaw CLI
# 模型二选一：PPIO 云端 qwen80binstruct（默认，在 .env 写 PPIO_API_KEY=...）/ 本机 ollama pull qwen3:14b
```

**1. 一次性配置隔离 profile**（默认自动使用项目 `.venv`；不影响用户默认 profile）
```bash
OPENCLAW_BIN=/opt/homebrew/bin/openclaw bash openclaw/setup.sh
# 写 ~/.openclaw-multiap/openclaw.json + 注册 MCP multiap-tools，末尾 config validate 通过即成功
```

**2. ns-3 模式运行**（仿真实验数据必须由 ns-3 上报）

> 先 `bash openclaw/serve.sh start` 拉起常驻服务（state server + gateway + Dashboard + 曲线窗），再跑 `run_openclaw.py`。默认 ns-3 路径会托管 live 仿真：读取 ns-3 `TELEMETRY`、写入 state server，并把最终决策通过 ns-3 stdin `APPLY` 写回仿真。状态服务器只接受 `source=ns3` 或 `source=ap`。

```bash
# ① 常驻服务
bash openclaw/serve.sh start

# ② 触发协商；默认启动 /Users/heyu/Developer/ns-3.47 的 live ns-3 并写回 APPLY
.venv/bin/python run_openclaw.py --data-source ns3 \
  --ns3-scenario line --ns3-business-profile live_bulk \
  --no-academic-plot --no-dashboard --max-steps 24
```

外部 ns-3/日志转发模式只用于调试：`--ns3-external` 配合 `state_server/ns3_bridge.py`。该模式仍要求输入来自真实 ns-3 输出，bridge 不生成、不扰动 QoS；但 stdin 写回只在默认托管 live ns-3 路径中自动完成。

> 🚦 **coordinator 已默认停用（2026-06）**：`run_openclaw.py` 现在默认**进程内直接跑阶段接力**（`structured_relay`），
> 不再启动 coordinator LLM agent。原因：coordinator 对协商**零功能贡献**——发言顺序（广播 ap1→ap2→ap3、
> ap1 提案、ap2/ap3 投票、ap1 收口决策、反对即接管）全部固定在 `orchestration.py` 的 `structured_relay` 里，
> coordinator 只是用 `--local` 冷启动一个 LLM 去调一次 `run_fast_negotiation` 并回显结果，平白多出
> **~60s（冷启动 ~13s + 2 次 LLM 调用）**。需要回到旧路径做对比时加 `--use-coordinator`（见下方 §3）。

**3. （旧路径，已停用）直接触发 coordinator**（仅做对比/调试用；默认路径无需此步）
```bash
# 经薄启动器回退到 coordinator 路径：
.venv/bin/python run_openclaw.py --data-source ns3 --use-coordinator
# 或不经启动器手动触发 coordinator：
OLLAMA_API_KEY=ollama-local NO_PROXY=localhost,127.0.0.1,::1 \
  openclaw --profile multiap agent --local --agent coordinator \
  -m "开始协商，请直接调用 run_fast_negotiation 控制总耗时" --json
```

**4. 真实 AP 模式**
```bash
.venv/bin/python state_server/server.py                                               # ① 状态服务器（启动一次）
.venv/bin/python state_server/reporter.py --ap-id ap1 --server http://<DGX_IP>:5001    # ② 各香蕉派上报（ap1/ap2/ap3）
.venv/bin/python run_openclaw.py --data-source real --server http://localhost:5001 \
  --ap-endpoints ap1=192.168.1.1:5002,ap2=192.168.1.2:5002,ap3=192.168.1.3:5002        # ③ 触发并下发决策
#  或 --ap-config ap_endpoints.json（须显式指定；不再自动读取）
```

**测试**
```bash
.venv/bin/python -m unittest discover -s tests          # 确定性套件 16/16
```

常用开关：`--data-source {ns3,real}` · `--ns3-scenario {line,triangle,asym}` · `--ns3-business-profile {live_bulk,mixed_qoe,deadline_backup,uniform}` · `--ns3-external` · `--no-academic-plot` · `--no-dashboard` · `--use-coordinator`（回退到旧 coordinator 触发路径，仅对比用） · `--require-qwen80b` · `--observation-wait <秒>`。
`run_openclaw.py` 内部已自动设 `OLLAMA_API_KEY` / `NO_PROXY`，第 2、4 节无需手动加；仅第 3 节直调 `openclaw` 时需要带上。

### 后台常驻服务（一条命令全开，协商零临时启动）

OpenClaw 的 `agent --local` 每个回合都冷启动一份 runtime + MCP server；state server / Dashboard / 曲线窗若每次临时起也有启动开销。`serve.sh` 把**所有可常驻的服务绑成一条命令**，`run_openclaw.py` 强制复用它们——协商时零临时服务启动。OpenClaw 已为 `multiap` profile 注册 launchd 网关服务 `ai.openclaw.multiap`（端口 18789，`RunAtLoad + KeepAlive`，开机自启/崩溃自拉起，本身就是长期服务）。

```bash
bash openclaw/serve.sh start     # 一条命令全开：state server(5001) + gateway(18789) + Dashboard(5050) + 学术曲线窗
bash openclaw/serve.sh status    # 查看四者状态
bash openclaw/serve.sh stop      # 停曲线/State/Dashboard；gateway 由 launchd 托管不强停（如需停用 launchctl bootout）
bash openclaw/serve.sh restart   # 改过 setup.sh/MCP 注册/配置后重载 gateway（否则缓存旧 MCP 连接，AP 调工具报 "tool isn't available"）
```

- **先 `serve.sh start` 再跑 `run_openclaw.py`**：默认路径（`structured_relay`）启动时强制检测 state server / gateway / Dashboard 在线，任一不在线即报错提示先 `serve.sh start`，不再临时起兜底——保证协商走热 gateway、Dashboard 实时可见。`--no-dashboard` 可主动跳过 Dashboard；`--use-coordinator` 路径走 `--local`，不检测 gateway。
- gateway 端口取自 profile 配置 `gateway.port`（默认 18789）；`serve.sh` 优先复用 launchd 服务，缺失时才 nohup 兜底，**不另起竞争 gateway、不碰其它 profile**。`drive_ap` 运行时若 gateway 连接失败会回退 `--local`（保底）。
- **学术曲线窗（matplotlib）也常驻**：`serve.sh start` 起一个常驻窗口，`run_openclaw.py` 检测到即复用（省每次 matplotlib 冷启动 ~2-3s），未在线则跳过提示。无桌面/SSH 环境自动跳过；`--no-academic-plot` 可主动关。
- **Dashboard 实时对话流**：常驻 Dashboard 是独立进程，`run_openclaw.py` 把会话事件经 HTTP `POST /push` 推给它，再由 SSE 广播到浏览器——不再依赖进程内 `push_event`，常驻 Dashboard 也能看到实时对话/投票/决策（终端不再有 `Serving Flask app` 噪声）。
- `serve.sh` 起的是裸 state server，只接受 `source=ns3` 或 `source=ap`。数据新鲜度由托管 ns-3 live、外部 ns-3 bridge 或香蕉派 reporter 维持。
- **提速预期**：省掉每回合 runtime/provider/MCP 冷启动 + 各服务临时启动；**不缩短模型推理本身**（每回合 ~13s 不变），整体收益取决于冷启动占比。

---

## 架构总览

```
┌──────────────────────────────────────────────────────────────┐
│ OpenClaw（隔离 profile: multiap，~/.openclaw-multiap/）        │
│                                                              │
│  托管层 agents：ap1 / ap2 / ap3（coordinator 默认停用）       │
│    模型：PPIO qwen80binstruct（默认）/ ollama qwen3:14b（回退）│
│                                                              │
│  入口（默认）：run_openclaw.py 进程内直接调 structured_relay  │
│    └─ 固定顺序驱动 ap1/ap2/ap3，免 coordinator 冷启动开销      │
│  coordinator（LLM，已停用，--use-coordinator 回退）           │
│    └─ 旧路径：调 MCP run_fast_negotiation 一次性推进协议       │
│                                                              │
│  MCP 工具服务 multiap-tools（openclaw/mcp/multiap_mcp.py）：   │
│    get_latest_ap_states / analyze_sr_interference /          │
│    compute_sr_feasible_ranges / select_sr_concurrent_groups /│
│    evaluate_sr_candidate / rank_sr_candidates /              │
│    validate_edca_proposal（AP 可调用）                        │
│    run_fast_negotiation                                      │
│      （仅 coordinator，AP 经 per-agent tools.deny 禁用）       │
└──────────────────────────────────────────────────────────────┘
        │ MCP stdio                       │ openclaw agent --local
        ▼                                 ▼
  src/tools{sr,edca} · validator ·   state_server（Flask）
  profile · state_client            ← ns3_bridge / reporter
```

编排「机制层」（阶段指令、驱动 AP、计票、反提案接管、Validator 重试与终止）实现在
`openclaw/mcp/orchestration.py` 的 `structured_relay`，由 `run_fast_negotiation` 工具内部调用。

---

## 实验数据源

系统只保留两类数据源：

| 来源 | `--data-source` | `/state` source | 用途 |
|------|-----------------|-----------------|------|
| ns-3 | `ns3` | `ns3` | 仿真实验、tool 分级对比 |
| 真实 AP | `real` | `ap` | 香蕉派实测 |

协商路径由提案 AP 基于实时状态与工具验算自主选择，不按 AP 编号或固定业务身份预设。

---

## 协商流程（四阶段，由 `structured_relay` 编排）

```
阶段 1 广播   ap1→ap2→ap3 依次播报自身实测数据（只报己方数据 + 本机扫描的邻居 RSSI）
    ↓
阶段 2 提案   首轮固定由 ap1 发起、自主选路；提案前必须 get_latest_ap_states，
              Co-SR 先 analyze_sr_interference→select_sr_concurrent_groups，提交前自检
    ↓
阶段 3 投票   非提案 AP 逐一表态 同意/弃权/反对；反对者当场给反提案并接管为新提案方
              （反提案 JSON 解析失败时触发一次「修复轮」补纯 JSON）
    ↓
（如未通过）  Validator 未过则写回原因，从 ap1 重提案，最多 3 轮
    ↓
阶段 4 决策   提案方输出最终 JSON → 确定性 Validator 验算 → 通过则下发执行
```

终止保证：重投上限 `MAX_VOTE_ROUNDS=3`、验证重试 3、单轮最大发言 30，不收敛时干净退出。

### Agent 可调用的 MCP 工具

| 工具名 | 阶段 | 作用 |
|--------|------|------|
| `get_latest_ap_states` | 提案 / 投票 | 获取全部 AP 最新参数状态；提案或投票前必须先调用 |
| `analyze_sr_interference` | Co-SR 提案 | 分析强/中等干扰链路、主要干扰源和受害 AP |
| `compute_sr_feasible_ranges` | Co-SR 提案 | 计算每个 AP 的 TX Power 可行区间 |
| `select_sr_concurrent_groups` | Co-SR 提案 | 选择空间复用并发组并给出组内推荐功率 |
| `evaluate_sr_candidate` | Co-SR 提案 / 投票 | 评估候选功率是否满足 CCA/SINR/STA-RSSI 约束 |
| `rank_sr_candidates` | Co-SR 提案 | 对多个候选功率方案按目标排序 |
| `validate_edca_proposal` | 提案 / 投票 | 验算 EDCA 参数合法性、优先级单调性与拥塞匹配度 |

coordinator 专用（AP 经 per-agent `tools.deny` 禁用）：`run_fast_negotiation`。
工具实现见 `openclaw/mcp/multiap_mcp.py`，复用 `src/tools/`、`src/validator.py` 等保留的确定性 Python。

---

## 项目结构

```
.
├── run_openclaw.py               # 薄启动器：读取 ns-3/真实遥测 → 复用常驻服务 → 进程内直跑 structured_relay
├── openclaw/
│   ├── setup.sh                  # 配置隔离 profile multiap（providers + 4 agent + 工具限制 + MCP 注册）
│   ├── scenes.py                 # 通用执行端点解析 helper
│   ├── mcp/
│   │   ├── multiap_mcp.py        # stdio MCP 工具服务（暴露计算/验算/状态/编排/下发工具）
│   │   ├── orchestration.py      # 编排机制层：四阶段 structured_relay、驱动 AP、计票、反提案
│   │   ├── proposal_utils.py     # 提案/JSON/策略推断纯函数
│   │   └── tool_console.py       # 工具调用富文本 formatter（阶段接力工具展示）
│   └── workspaces/<agent>/       # 各 agent 的 IDENTITY/SOUL/AGENTS/TOOLS.md
├── state_server/
│   ├── server.py                 # Flask 状态服务器（AP 上报 / MCP 工具读取）
│   ├── ns3_bridge.py             # ns-3 遥测 JSONL → state server
│   ├── reporter.py               # AP 状态上报脚本（部署在香蕉派）
│   ├── executor.py               # 执行端点：接收决策并下发到硬件
│   └── academic_plot.py          # Matplotlib 学术曲线窗口
├── src/                          # 保留的确定性基础设施/工具（被 MCP 工具复用）
│   ├── tools/{sr,edca}.py        # Co-SR / Co-EDCA 计算与约束验算
│   ├── validator.py              # 确定性 Validator：物理约束最终验算
│   ├── profile.py                # 状态规范化 + 字段白名单
│   ├── state_client.py           # 读取状态服务器（含过期检查）
│   ├── logger.py                 # 结构化 JSONL 日志
│   └── console_style.py          # 彩色终端输出
├── dashboard/                    # Flask + SSE 实时协商对话 Dashboard
├── logs/                         # 每次运行生成一个 session_*.jsonl
└── docs/                         # 设计文档（docs/openclaw/ 为 OpenClaw 自身参考文档）
```

---

## 日志

每次运行在 `logs/` 生成一个 JSONL 文件，每行一个事件
（`session_start` / `phase_start` / `agent_speak` / `tool_call` / `vote` / `round_result` /
`final_decision` / `validation_result` / `executor_apply` / `session_end`），供 Dashboard 可视化或离线分析。

---

## 物理约束（Validator 检查项）

**Co-SR**
- `TX Power` ∈ [1, 23] dBm，功率调整量相对协商前为整数 dB
- CCA（邻居接收信号）/ SINR / 降功率后 STA RSSI 约束（由计算工具保证）

**Co-EDCA**
- `CWmin` ∈ [3, 1023]，`CWmax` ∈ [7, 1023]，`AIFSN` ∈ [1, 15]，`CWmax > CWmin`
- 优先级单调性：`high.CWmin ≤ medium ≤ low`（AIFSN 同理）

---

## 测试

```bash
.venv/bin/python -m unittest discover -s tests
# 覆盖：三场景结构等价、反提案修复轮、MCP 提案回填、状态服务器、Dashboard、学术曲线
```
