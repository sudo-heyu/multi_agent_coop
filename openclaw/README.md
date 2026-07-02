# OpenClaw 集成

OpenClaw 是 AP agent 的托管与工具运行时；默认阶段编排入口是项目内的 Python `structured_relay`，不是 coordinator agent。

## 组件

- `ap1 / ap2 / ap3`：独立 workspace 和临时 session 的 OpenClaw agent。
- `coordinator`：保留的兼容 agent，仅供 `--use-coordinator` 对比路径使用。
- `mcp/multiap_mcp.py`：向 AP 暴露状态、Co-SR、Co-EDCA 工具；另向 coordinator 暴露 `run_fast_negotiation`。
- `mcp/orchestration.py`：确定性的阶段轮转和 AP 驱动，被默认入口直接调用。
- `setup.sh`：创建隔离 profile `multiap`、四个 agent、模型 provider 和 MCP 注册。
- `serve.sh`：管理常驻 state server、gateway、Dashboard 和学术曲线窗。

模型默认使用 PPIO `qwen80binstruct`；未配置 PPIO key 时回退本地 Ollama `qwen3:14b`。配置位于 `~/.openclaw-multiap/`，不影响默认 profile。

## 一次性配置

```bash
pip install -r requirements.txt
npm install -g openclaw
MULTIAP_PY="$PWD/.venv/bin/python" bash openclaw/setup.sh
```

## 默认运行

```bash
bash openclaw/serve.sh start
bash openclaw/serve.sh status
.venv/bin/python run_openclaw.py --scene joint
```

`run_openclaw.py` 强制复用常驻服务：state server 必须在线；默认路径要求 gateway 在线；未传 `--no-dashboard` 时 Dashboard 也必须在线。plot 是可选服务，缺失只提示、不阻塞。

`drive_ap` 优先连接常驻 gateway；单个回合连接失败时会回退 `--local`。广播的三个模型回合并发执行，输出仍按 ap1、ap2、ap3 排序。非广播回合通过 session/raw-stream JSONL 把文本增量和工具调用实时送到终端与 Dashboard。

## 服务管理

```bash
bash openclaw/serve.sh start
bash openclaw/serve.sh status
bash openclaw/serve.sh restart  # 配置或 MCP 变更后重载 gateway
bash openclaw/serve.sh stop
```

`serve.sh` 优先复用 launchd 管理的 `ai.openclaw.multiap` gateway；缺失时才使用 nohup 兜底。`stop` 不停止 launchd 托管的 gateway。Dashboard 通过本机 `POST /push` 接收 `SessionLogger` 事件，再通过 SSE 广播。

## coordinator 兼容路径

```bash
.venv/bin/python run_openclaw.py --scene edca --use-coordinator
```

此路径用 `--local` 启动 coordinator，后者调用一次 `run_fast_negotiation`。它与默认路径共享同一套 `structured_relay`，但多一次 coordinator 模型启动和汇总，因此只用于兼容与性能对比。

## 测试

```bash
.venv/bin/python -m unittest discover -s tests
```

当前确定性套件为 48/48。真实模型端到端测试依赖已配置的 provider、运行中的常驻服务和可用状态数据，不属于该单元测试命令。
