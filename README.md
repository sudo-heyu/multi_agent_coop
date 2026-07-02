# Multi-AP 协商系统（OpenClaw Agent + 确定性 Python 编排）

多台 Wi-Fi AP 通过 LLM Agent 自主协商，协调发射功率（Co-SR）和 MAC 退避参数（Co-EDCA），提升整体网络性能。

**架构**：`ap1 / ap2 / ap3` 是由 OpenClaw 托管的独立 agent；默认入口是 Python 启动器 `run_openclaw.py`，它在进程内调用 `structured_relay` 完成确定性的阶段轮转。Co-SR/Co-EDCA 计算工具经 MCP 暴露给 AP，最终 Validator、状态读取和执行下发仍是确定性 Python。`coordinator` agent 仅保留为 `--use-coordinator` 兼容路径，不参与默认运行。

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

> 先 `bash openclaw/serve.sh start` 拉起常驻服务（state server + gateway + Dashboard + 曲线窗），再跑 `run_openclaw.py`——默认路径强制复用它们，不在线会报错提示先 `serve.sh start`。详见下方「后台常驻服务」。

```bash
# 演示：复用常驻曲线窗 + Dashboard，协商后曲线体现改善
.venv/bin/python run_openclaw.py --scene joint

# 无头快速验证（本次三场景实测即此）
.venv/bin/python run_openclaw.py --scene edca --no-academic-plot --no-dashboard --exit-after-run --max-steps 24
```
> ⚠️ **不要加 `--no-feeder`** —— 它只推一帧，长协商时状态会过期（`StateStaleError`）导致失败；需连续喂数器保持状态新鲜。

> 🚦 **coordinator 已默认停用（2026-06）**：`run_openclaw.py` 现在默认**进程内直接跑阶段接力**（`structured_relay`），
> 不再启动 coordinator LLM agent。原因：coordinator 对协商**零功能贡献**——发言顺序（广播 ap1→ap2→ap3、
> ap1 提案、ap2/ap3 投票、ap1 收口决策、反对即接管）全部固定在 `orchestration.py` 的 `structured_relay` 里，
> coordinator 只是用 `--local` 冷启动一个 LLM 去调一次 `run_fast_negotiation` 并回显结果，平白多出
> **~60s（冷启动 ~13s + 2 次 LLM 调用）**。需要回到旧路径做对比时加 `--use-coordinator`（见下方 §3）。

**3. （旧路径，已停用）直接触发 coordinator**（仅做对比/调试用；默认路径无需此步）
```bash
# 经薄启动器回退到 coordinator 路径：
.venv/bin/python run_openclaw.py --scene edca --use-coordinator
# 或不经启动器手动触发 coordinator：
OLLAMA_API_KEY=ollama-local NO_PROXY=localhost,127.0.0.1,::1 \
  openclaw --profile multiap agent --local --agent coordinator \
  -m "开始协商，请直接调用 run_fast_negotiation 控制总耗时" --json
```

**4. 真实 AP 模式**
```bash
# DGX：以真实数据策略启动常驻服务（state server 拒收 mock/generated）
MULTIAP_STATE_MODE=real bash openclaw/serve.sh restart

# 各香蕉派：启动 reporter 和 executor（ap-id/地址按机器修改）
.venv/bin/python state_server/reporter.py --ap-id ap1 --server http://<DGX_IP>:5001
.venv/bin/python state_server/executor.py --ap-id ap1 --port 5002

# DGX：触发协商并明确配置执行端点
.venv/bin/python run_openclaw.py --mode real --server http://localhost:5001 \
  --ap-endpoints ap1=192.168.1.1:5002,ap2=192.168.1.2:5002,ap3=192.168.1.3:5002        # ③ 触发并下发决策
#  或 --ap-config ap_endpoints.json（须显式指定；不再自动读取，避免 mock 演示误推到不可达 AP 而 8s 超时）
```

`--mode real` 不创建 `MockTelemetryFeeder`，会等待 ap1/ap2/ap3 均有未过期的 `source=ap` 状态，并强制要求三个 executor 端点。若 state server 允许 mock，启动器会拒绝继续并提示按真实模式重启服务。

**测试**
```bash
.venv/bin/python -m unittest discover -s tests          # 当前确定性套件 68/68
```

常用开关：`--mode {mock,real}` · `--scene {sr,edca,joint}` · `--state-wait <秒>` · `--no-academic-plot` · `--no-dashboard` · `--exit-after-run`（跑完即退） · `--use-coordinator`（回退到旧 coordinator 触发路径，仅对比用） · `--require-qwen80b` · `--observation-wait <秒>`。
`run_openclaw.py` 内部已自动设 `OLLAMA_API_KEY` / `NO_PROXY`，第 2、4 节无需手动加；仅第 3 节直调 `openclaw` 时需要带上。

### 后台常驻服务（一条命令全开，协商零临时启动）

OpenClaw 的 `agent --local` 每个回合都冷启动一份 runtime + MCP server；state server / Dashboard / 曲线窗若每次临时起也有启动开销。`serve.sh` 把**所有可常驻的服务绑成一条命令**，`run_openclaw.py` 强制复用它们——协商时零临时服务启动。OpenClaw 已为 `multiap` profile 注册 launchd 网关服务 `ai.openclaw.multiap`（端口 18789，`RunAtLoad + KeepAlive`，开机自启/崩溃自拉起，本身就是长期服务）。

```bash
MULTIAP_STATE_MODE=mock bash openclaw/serve.sh start   # mock，state 接受生成数据（默认）
MULTIAP_STATE_MODE=real bash openclaw/serve.sh restart # real，state 拒收生成数据
bash openclaw/serve.sh status    # 查看五者状态（state / gateway / dashboard / harvester / plot）
bash openclaw/serve.sh stop      # 停曲线/State/Dashboard/harvester；gateway 由 launchd 托管不强停（如需停用 launchctl bootout）
bash openclaw/serve.sh restart   # 改过 setup.sh/MCP 注册/配置后重载 gateway（否则缓存旧 MCP 连接，AP 调工具报 "tool isn't available"）
```

- **先 `serve.sh start` 再跑 `run_openclaw.py`**：默认路径（`structured_relay`）启动时强制检测 state server / gateway / Dashboard 在线，任一不在线即报错提示先 `serve.sh start`，不再临时起兜底——保证协商走热 gateway、Dashboard 实时可见。`--no-dashboard` 可主动跳过 Dashboard；`--use-coordinator` 路径走 `--local`，不检测 gateway。
- gateway 端口取自 profile 配置 `gateway.port`（默认 18789）；`serve.sh` 优先复用 launchd 服务，缺失时才 nohup 兜底，**不另起竞争 gateway、不碰其它 profile**。`drive_ap` 运行时若 gateway 连接失败会回退 `--local`（保底）。
- **学术曲线窗（matplotlib）也常驻**：`serve.sh start` 起一个常驻窗口，`run_openclaw.py` 检测到即复用（省每次 matplotlib 冷启动 ~2-3s），未在线则跳过提示。无桌面/SSH 环境自动跳过；`--no-academic-plot` 可主动关。
- **Dashboard 实时对话流**：常驻 Dashboard 是独立进程，`run_openclaw.py` 把会话事件经 HTTP `POST /push` 推给它，再由 SSE 广播到浏览器——不再依赖进程内 `push_event`，常驻 Dashboard 也能看到实时对话/投票/决策（终端不再有 `Serving Flask app` 噪声）。
- **Outcome 收割器常驻**：`serve.sh start` 拉起 `state_server/outcome_harvester.py`，每 `MULTIAP_HARVEST_INTERVAL`（默认 30s）结算到期的效果评估窗口、放弃逾期太久的窗口——real 模式长评估窗口（可达 15 分钟）不再依赖下次协商即可自动结算，是记忆效果反馈在真实部署下可靠的前提。
- `MULTIAP_STATE_MODE=mock` 时 state server 带 `--allow-mock`；`real` 时不带。数据新鲜度分别由 feeder 或香蕉派 reporter 维持。
- **提速预期**：省掉每回合 runtime/provider/MCP 冷启动 + 各服务临时启动；**不缩短模型推理本身**（每回合 ~13s 不变），整体收益取决于冷启动占比。

---

## 架构总览

```text
run_openclaw.py（默认入口）
  └─ structured_relay（Python 确定性阶段机制）
       ├─ OpenClaw gateway → ap1 / ap2 / ap3
       │                         └─ multiap-tools MCP → src/tools + state server
       ├─ src/validator.py
       ├─ SessionLogger → JSONL / Dashboard
       └─ 可选 executor /apply

--use-coordinator 兼容入口
  └─ coordinator agent → run_fast_negotiation MCP → 同一个 structured_relay
```

编排「机制层」（阶段指令、驱动 AP、计票、反提案接管、Validator 重试与终止）实现在
`openclaw/mcp/orchestration.py` 的 `structured_relay`。默认由 `run_openclaw.py` 直接调用；仅兼容 coordinator 路径通过 MCP 工具 `run_fast_negotiation` 间接调用。

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
阶段 1 广播   ap1/ap2/ap3 并发生成回复，再按 ap1→ap2→ap3 顺序记录和展示
    ↓
阶段 2 提案   首轮固定由 ap1 发起、自主选路；提案前必须 get_latest_ap_states，
              Co-SR 先 analyze_sr_interference→select_sr_concurrent_groups，提交前自检
    ↓
阶段 3 投票   非提案 AP 逐一表态 同意/弃权/反对；反对者当场给反提案并接管为新提案方
              （反提案 JSON 解析失败时触发一次「修复轮」补纯 JSON）
    ↓
（如未通过）  Validator 未过则写回原因，从 ap1 重提案，最多 3 轮
    ↓
阶段 4 决策   系统直接采用已获通过的提案 JSON（不再调用 LLM）→ Validator 验算 → 通过则下发
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
├── run_openclaw.py               # 薄启动器：准备场景 → 复用 serve.sh 常驻服务（state/gateway/Dashboard/曲线）→ 进程内直跑 structured_relay（coordinator 默认停用，--use-coordinator 回退）
├── openclaw/
│   ├── setup.sh                  # 配置隔离 profile multiap（providers + 4 agent + 工具限制 + MCP 注册）
│   ├── scenes.py                 # 三套 mock 场景 + 状态服务器/Dashboard/学术曲线启动器
│   ├── mcp/
│   │   ├── multiap_mcp.py        # stdio MCP 工具服务（暴露计算/验算/状态/编排/下发工具）
│   │   ├── orchestration.py      # 编排机制层：四阶段 structured_relay、驱动 AP、计票、反提案
│   │   ├── proposal_utils.py     # 提案/JSON/策略推断纯函数
│   │   └── tool_console.py       # 工具调用富文本 formatter（阶段接力工具展示）
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
├── memory_admin.py               # 本地 SQLite 事件/恢复 checkpoint 查询
├── logs/                         # JSONL + agent_memory.sqlite3 持久事件存储
└── docs/                         # 设计文档（docs/openclaw/ 为 OpenClaw 自身参考文档）
```

---

## 日志

每次运行在 `logs/` 生成一个 JSONL 文件，每行一个事件
（`session_start` / `phase_start` / `agent_speak` / `tool_call` / `vote` / `round_result` /
`final_decision` / `validation_result` / `executor_apply` / `session_end`），供 Dashboard 可视化或离线分析。

同一事件还会双写到 `logs/agent_memory.sqlite3`，按 run 保存有序事件和状态快照。可用
`.venv/bin/python memory_admin.py incomplete` 查看异常中断运行，或用
`.venv/bin/python memory_admin.py show <run_id>` 回放。executor 下发已使用持久化 action journal 和幂等 key：成功动作不会重复发送，明确失败最多尝试两次，网络不确定结果会阻塞恢复，必须核对 AP `/status` 后执行 `memory_admin.py resolve-action`。记忆模块完整说明见 `docs/memory-module.md`，演进历史见 `docs/memory-architecture.md`。

异常退出且 checkpoint 安全时，可执行 `.venv/bin/python run_openclaw.py --resume-run <run_id>`。恢复会跳过已完成的广播、提案和投票，只继续未完成边界；若 AP 参数、业务优先级或邻居拓扑已变化，启动器拒绝恢复并要求创建新协商。

长会话使用持久化 Session Memory：早期 transcript 被确定性压缩为带 speaker/kind/turn 的摘要，最近发言保留原文，每个 AP 回合默认限制为 14000 字符。可通过 `--context-budget-chars` 和 `--context-recent-turns` 调整；原始事件和完整 transcript 仍保存在 SQLite/JSONL，不因上下文压缩丢失。

每次 run 结束会自动生成 Episodic Memory，保存初始环境、领域特征、决策、Validator、执行结果和观测指标。下一次同拓扑协商会检索最多 3 个高质量相似案例注入提案提示，但历史参数不能绕过最新状态读取和工具验算。可用 `memory_admin.py episodes` 和 `memory_admin.py similar <run_id>` 查询。

决策生效后还会登记多时间窗口的 Outcome 评估（`--eval-windows`，默认 mock=10,30s / real=60,300,900s）：到期时与协商前基线比较吞吐/延迟/丢包，按业务优先级加权判定 `improved / degraded / neutral / inconclusive`，并把结论回写案例质量——实际恶化的案例质量封顶 0.2，不会再被当作高质量参考注入提案，同时生成恢复协商前参数的回滚建议（只建议，不自动执行）。收割不阻塞进程：mock 保活循环内实时结算，其余由下次 `run_openclaw.py` 启动时或 `memory_admin.py evaluate --server <url>` 补收；`memory_admin.py evaluations <run_id>` 查看窗口结论与回滚建议。

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
# 覆盖：三场景结构等价、反提案修复轮、MCP 提案回填、状态服务器、Dashboard、学术曲线、
#      事件存储迁移、幂等执行、Session/Episodic Memory、Outcome 评估与回滚建议
```
