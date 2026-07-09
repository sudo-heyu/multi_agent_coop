# OpenClaw 集成

OpenClaw 是 AP agent 的托管与工具运行时；默认阶段编排入口是项目内的 Python `structured_relay`，不是 coordinator agent。

## 组件

- `ap1 / ap2 / ap3`：独立 workspace 和临时 session 的 OpenClaw agent。
- `coordinator`：保留的兼容 agent，仅供 `--use-coordinator` 对比路径使用。
- `mcp/multiap_mcp.py`：向 AP 暴露状态、Co-SR、Co-EDCA 工具；另向 coordinator 暴露 `run_fast_negotiation`。
- `mcp/orchestration.py`：确定性的阶段轮转和 AP 驱动，被默认入口直接调用。

> mock 运行时模式已移除（2026-07）：数据来源仅 real（香蕉派）/ ns3（仿真桥）两种，`--mode` 必填；mock 场景与喂数器降级为测试夹具（tests/mock_scenes.py、tests/mock_feeder.py）。
- `setup.sh`：创建隔离 profile `multiap`、四个 agent、模型 provider 和 MCP 注册。
- `serve.sh`：管理常驻 state server、gateway、Dashboard 和学术曲线窗。

模型默认使用 PPIO API（`qwen80binstruct` alias，当前指向 PPIO 可用的后继模型）；未配置 PPIO key 时 setup 直接失败，不再回退本地 Ollama。只有显式 `MULTIAP_MODEL_REF=ollama/...` 并在运行时加 `--allow-ollama` / `MULTIAP_ALLOW_OLLAMA=1`，才允许使用本地 Ollama。配置位于 `~/.openclaw-multiap/`，不影响默认 profile。

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
.venv/bin/python run_openclaw.py --mode ns3 --scene sr
```

`run_openclaw.py` 强制复用常驻服务：state server 必须在线；默认路径要求 gateway 在线；未传 `--no-dashboard` 时 Dashboard 也必须在线。plot 是可选服务，缺失只提示、不阻塞。

`drive_ap` 优先连接常驻 gateway；单个回合连接失败时会回退 `--local`。广播的三个模型回合并发执行，输出仍按 ap1、ap2、ap3 排序。默认只展示整轮最终回复，避免继续追 session/raw-stream 文本文件；如需调试文本增量，可显式设置 `MULTIAP_OPENCLAW_RAW_STREAM=1` 或 `MULTIAP_OPENCLAW_SESSION_TAIL=1`。工具调用由 `multiap_mcp.py` 在工具函数源头写入 `tool-events.jsonl`，再实时送到终端与 Dashboard。

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
.venv/bin/python run_openclaw.py --mode ns3 --scene edca --use-coordinator
```

此路径用 `--local` 启动 coordinator，后者调用一次 `run_fast_negotiation`。它与默认路径共享同一套 `structured_relay`，但多一次 coordinator 模型启动和汇总，因此只用于兼容与性能对比。

## 测试

```bash
.venv/bin/python -m unittest discover -s tests
```

当前确定性套件为 246/246。真实模型端到端测试依赖已配置的 provider、运行中的常驻服务和可用状态数据，不属于该单元测试命令。
