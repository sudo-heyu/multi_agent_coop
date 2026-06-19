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

**1. 一次性配置隔离 profile**（必须用 .venv 的 python；不影响用户默认 profile）
```bash
MULTIAP_PY="$PWD/.venv/bin/python" OPENCLAW_BIN=/opt/homebrew/bin/openclaw bash openclaw/setup.sh
# 写 ~/.openclaw-multiap/openclaw.json + 注册 MCP multiap-tools，末尾 config validate 通过即成功
```

**2. Mock 模式运行**（无需真实 AP，`--scene` 可选 `sr` / `edca` / `joint`）
```bash
# 演示：弹出学术曲线 + Dashboard，协商后曲线体现改善
.venv/bin/python run_openclaw.py --scene joint

# 无头快速验证（本次三场景实测即此）
.venv/bin/python run_openclaw.py --scene edca --no-academic-plot --no-dashboard --exit-after-run --max-steps 24
```
> ⚠️ **不要加 `--no-feeder`** —— 它只推一帧，长协商时状态会过期（`StateStaleError`）导致失败；需连续喂数器保持状态新鲜。

**3. 直接触发 coordinator**（不经薄启动器，需先有状态服务器在喂数）
```bash
OLLAMA_API_KEY=ollama-local NO_PROXY=localhost,127.0.0.1,::1 \
  openclaw --profile multiap agent --local --agent coordinator \
  -m "开始协商，请直接调用 run_fast_negotiation 控制总耗时" --json
```

**4. 真实 AP 模式**
```bash
.venv/bin/python state_server/server.py                                               # ① 状态服务器（启动一次）
.venv/bin/python state_server/reporter.py --ap-id ap1 --server http://<DGX_IP>:5001    # ② 各香蕉派上报（ap1/ap2/ap3）
.venv/bin/python run_openclaw.py --server http://localhost:5001 \
  --ap-endpoints ap1=192.168.1.1:5002,ap2=192.168.1.2:5002,ap3=192.168.1.3:5002        # ③ 触发并下发决策
#  或 --ap-config ap_endpoints.json（默认自动读取仓库根）
```

**测试**
```bash
.venv/bin/python -m unittest discover -s tests          # 确定性套件 16/16
```

常用开关：`--scene {sr,edca,joint}` · `--no-academic-plot` · `--no-dashboard` · `--exit-after-run`（跑完即退） · `--direct-relay`（绕过 coordinator，仍用 OpenClaw AP agent） · `--require-qwen80b` · `--observation-wait <秒>`。
`run_openclaw.py` 内部已自动设 `OLLAMA_API_KEY` / `NO_PROXY`，第 2、4 节无需手动加；仅第 3 节直调 `openclaw` 时需要带上。

### 后台常驻服务（加速冷启动）

OpenClaw 的 `agent --local` 每个回合都冷启动一份 runtime + MCP server。让协商时的 AP 回合改走**常驻 gateway**
即可省掉这部分开销。OpenClaw 已为 `multiap` profile 注册了 launchd 网关服务 `ai.openclaw.multiap`
（端口 18789，`RunAtLoad + KeepAlive`，开机自启/崩溃自拉起，本身就是长期服务）。`serve.sh` 把它与 state server
绑成一条命令：

```bash
bash openclaw/serve.sh start     # 起 state server(5001) + 确保 multiap gateway(18789) 在线
bash openclaw/serve.sh status    # 查看两者状态
bash openclaw/serve.sh stop      # 停 state server；gateway 由 launchd 托管不强停（如需停用 launchctl bootout）
bash openclaw/serve.sh restart
```

- gateway 端口取自 profile 配置 `gateway.port`（默认 18789，与 launchd 服务一致）；`serve.sh` 优先复用 launchd 服务，缺失时才 nohup 兜底，**不另起竞争 gateway、不碰其它 profile**。
- 起了 gateway 后，直接照常 `run_openclaw.py` 跑场景即可：`orchestration.drive_ap` 探测到 gateway 在线就**自动**走它（AP 回合免冷启动），离线则回退 `--local`，无需额外参数。coordinator 入口仍走 `--local`（避免 MCP 实例重入死锁）。
- **提速预期**：主要省掉每回合的 runtime/provider/插件冷启动与 MCP 反复 spawn；**不会缩短 PPIO 推理本身**（每回合 ~13s 的模型时间不变），整体收益取决于冷启动占比。
- `serve.sh` 起的是裸 state server，数据新鲜度由喂数器（mock：`run_openclaw.py` 的连续喂数器）或香蕉派 reporter（真实）维持。

---

## 架构总览

```
┌──────────────────────────────────────────────────────────────┐
│ OpenClaw（隔离 profile: multiap，~/.openclaw-multiap/）        │
│                                                              │
│  托管层 agents：coordinator / ap1 / ap2 / ap3                 │
│    模型：PPIO qwen80binstruct（默认）/ ollama qwen3:14b（回退）│
│                                                              │
│  coordinator（LLM，阶段级入口）                               │
│    └─ 调用 MCP 工具 run_fast_negotiation 一次性推进协议        │
│       （不逐句选发言人，控制时延）                            │
│                                                              │
│  MCP 工具服务 multiap-tools（openclaw/mcp/multiap_mcp.py）：   │
│    get_latest_ap_states / analyze_sr_interference /          │
│    compute_sr_feasible_ranges / select_sr_concurrent_groups /│
│    evaluate_sr_candidate / rank_sr_candidates /              │
│    validate_edca_proposal（AP 可调用）                        │
│    run_fast_negotiation / validate_decision / push_decision  │
│      （仅 coordinator，AP 经 per-agent tools.deny 禁用）       │
└──────────────────────────────────────────────────────────────┘
        │ MCP stdio                       │ openclaw agent --local
        ▼                                 ▼
  src/tools{sr,edca} · validator ·   state_server（Flask）
  profile · state_client            ← reporter / mock 喂数器 / 预设场景
```

编排「机制层」（阶段指令、驱动 AP、计票、反提案接管、Validator 重试与终止）实现在
`openclaw/mcp/orchestration.py` 的 `structured_relay`，由 `run_fast_negotiation` 工具内部调用。

---

## 三个仿真场景

| 场景 | `--scene` | 触发条件 | 协商路径 |
|------|-----------|----------|----------|
| Co-SR | `sr` | 邻居 RSSI 偏强（强干扰） | 降低发射功率 |
| Co-EDCA | `edca` | 邻居弱、优先级/QoS 分化 | 调整 CWmin/CWmax/AIFSN |
| 联合 | `joint` | 高功率 + 优先级分化同时成立 | 联合调整，或先处理主导问题（由实时证据决定） |

路径由提案 AP 基于实时状态与工具验算自主选择，不按 AP 编号或固定业务身份预设。
场景定义见 `openclaw/scenes.py`（`MOCK_SCENE_SR/EDCA/JOINT`）。

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

coordinator 专用（AP 经 per-agent `tools.deny` 禁用）：`run_fast_negotiation`、`validate_decision`、`push_decision`。
工具实现见 `openclaw/mcp/multiap_mcp.py`，复用 `src/tools/`、`src/validator.py` 等保留的确定性 Python。

---

## 项目结构

```
.
├── run_openclaw.py               # 薄启动器：准备场景 → 拉起服务器/Dashboard/曲线 → 触发 coordinator
├── openclaw/
│   ├── setup.sh                  # 配置隔离 profile multiap（providers + 4 agent + 工具限制 + MCP 注册）
│   ├── scenes.py                 # 三套 mock 场景 + 状态服务器/Dashboard/学术曲线启动器
│   ├── mcp/
│   │   ├── multiap_mcp.py        # stdio MCP 工具服务（暴露计算/验算/状态/编排/下发工具）
│   │   ├── orchestration.py      # 编排机制层：四阶段 structured_relay、驱动 AP、计票、反提案
│   │   ├── proposal_utils.py     # 提案/JSON/策略推断纯函数
│   │   └── tool_console.py       # 工具调用富文本 formatter（--direct-relay 展示）
│   └── workspaces/<agent>/       # 各 agent 的 IDENTITY/SOUL/AGENTS/TOOLS.md
├── state_server/
│   ├── server.py                 # Flask 状态服务器（AP 上报 / MCP 工具读取）
│   ├── reporter.py               # AP 状态上报脚本（部署在香蕉派，或本地 mock）
│   ├── executor.py               # 执行端点：接收决策并下发到硬件
│   ├── mock_feeder.py            # mock 曲线喂数器（持续喂遥测 + 协商后注入决策）
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
