"""
纯 OpenClaw 架构入口 —— coordinator 触发阶段级快速协商。

用法：
  python run_openclaw.py --scene joint            # mock A：预设场景
  python run_openclaw.py --scene sr --max-steps 20
  python run_openclaw.py --scene edca --no-feeder  # 不喂曲线，仅跑协商

前置：openclaw 已装、ollama 运行、已执行过 `bash openclaw/setup.sh`。
"""
import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent / "openclaw" / "mcp"))

import requests

from run import MOCK_SCENES, start_mock_server
from state_server.mock_feeder import MockTelemetryFeeder
import orchestration as orch


OPENCLAW_BIN = os.environ.get("OPENCLAW_BIN", str(Path.home() / ".openclaw" / "bin" / "openclaw"))
OPENCLAW_PROFILE = os.environ.get("MULTIAP_PROFILE", "multiap")


def _print_event(phase, who, reply):
    print(f"\n{'═'*70}\n[{phase}] {who.upper()}")
    print(reply.strip())


def main():
    ap = argparse.ArgumentParser(description="多 AP 协商（纯 OpenClaw / coordinator 阶段级触发）")
    ap.add_argument("--scene", choices=["sr", "edca", "joint"], default="joint")
    ap.add_argument("--server", default="http://localhost:5001")
    ap.add_argument("--max-steps", type=int, default=24)
    ap.add_argument("--no-feeder", action="store_true", help="不启动曲线喂数器")
    ap.add_argument("--direct-relay", action="store_true",
                    help="调试用：绕过 coordinator，直接运行阶段接力")
    args = ap.parse_args()

    scene = MOCK_SCENES[args.scene]
    print(f"[run_openclaw] scene={args.scene} server={args.server} max_steps={args.max_steps}", flush=True)

    feeder = None
    ready, proc = start_mock_server(args.server)
    if not ready:
        print("[错误] 状态服务器未就绪。请先 `python3 state_server/server.py --allow-mock`。")
        sys.exit(1)
    if not args.no_feeder:
        feeder = MockTelemetryFeeder(args.server, scene, interval=1.0)
        feeder.start()
    else:
        # 不喂曲线也要保证状态可读：单次写入场景
        MockTelemetryFeeder(args.server, scene, interval=1.0).start()
    time.sleep(2.0)  # 等首批遥测落库

    orch.STATE_SERVER = args.server
    t0 = time.time()
    if args.direct_relay:
        result = orch.structured_relay(on_event=_print_event)
    else:
        _require_qwen80b_config()
        result = _run_via_coordinator(args.max_steps)
    dur = time.time() - t0

    print(f"\n{'━'*70}\n[结果] outcome={result['outcome']} turns={result['transcript_turns']} 用时 {dur:.0f}s")
    print(f"[策略] {result['strategy']}")
    print(f"[最终决策] {result['decision']}")
    v = result["validation"]
    if v:
        print(f"[Validator] {'✅ 通过' if v['approved'] else '❌ 未通过'} — {v['summary']}")
    else:
        print("[Validator] 无可验收决策（协商未收敛或未解析出决策 JSON）")

    if feeder is not None and result["decision"]:
        feeder.apply_decision(result["decision"])
        print("[Mock] 已将决策注入遥测，曲线将体现协商后改善。Ctrl-C 退出。")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
    if feeder is not None:
        feeder.stop()


def _run_via_coordinator(max_steps: int) -> dict:
    env = dict(os.environ)
    env.setdefault("OLLAMA_API_KEY", "ollama-local")
    env["NO_PROXY"] = _merge_no_proxy(env.get("NO_PROXY"))
    env["no_proxy"] = env["NO_PROXY"]
    message = (
        "开始一次多 AP 协商。为控制时间，请直接调用 run_fast_negotiation，"
        f"参数 max_validation_retries=3, max_turns={max_steps}。"
        "工具返回后只汇总 outcome、strategy、validation、decision，不要逐句选择发言人。"
        "最后必须附一个 json 代码块，原样包含工具返回结果，且保留 outcome、strategy、decision、"
        "validation、transcript_turns 字段。"
    )
    print("[OpenClaw] 启动 coordinator，调用 run_fast_negotiation；AP 对话会在工具内部批量执行。", flush=True)
    proc = subprocess.Popen(
        [OPENCLAW_BIN, "--profile", OPENCLAW_PROFILE, "agent", "--local",
         "--agent", "coordinator", "--thinking", "off", "--message", message, "--json"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env,
    )
    started = time.time()
    while proc.poll() is None:
        elapsed = int(time.time() - started)
        if elapsed and elapsed % 10 == 0:
            print(f"[OpenClaw] coordinator 仍在运行，已用时 {elapsed}s。qwen80binstruct + 多 AP 工具调用可能需要等待。", flush=True)
        if elapsed > 1800:
            proc.kill()
            stdout, stderr = proc.communicate()
            print(stderr or stdout)
            print("[错误] coordinator 超过 1800s 未结束。")
            sys.exit(1)
        time.sleep(1)
    stdout, stderr = proc.communicate()
    if proc.returncode != 0:
        print(stderr or stdout)
        sys.exit(proc.returncode)
    result = _extract_fast_result(stdout.strip())
    if result is None:
        print(stdout)
        print("[错误] coordinator 未返回可解析的 run_fast_negotiation 结果。")
        sys.exit(1)
    return result


def _merge_no_proxy(current: str | None) -> str:
    required = ["localhost", "127.0.0.1", "::1"]
    values = [v.strip() for v in (current or "").split(",") if v.strip()]
    for item in required:
        if item not in values:
            values.append(item)
    return ",".join(values)


def _require_qwen80b_config() -> None:
    cfg = Path.home() / f".openclaw-{OPENCLAW_PROFILE}" / "openclaw.json"
    try:
        data = json.loads(cfg.read_text(encoding="utf-8"))
    except FileNotFoundError:
        print(f"[错误] 未找到 OpenClaw profile 配置：{cfg}")
        print("请先运行：bash openclaw/setup.sh")
        sys.exit(1)

    defaults = data.get("agents", {}).get("defaults", {})
    primary = (defaults.get("model") or {}).get("primary")
    models = defaults.get("models") or {}
    alias_refs = {
        ref for ref, spec in models.items()
        if isinstance(spec, dict) and spec.get("alias") == "qwen80binstruct"
    }
    ppio_models = data.get("models", {}).get("providers", {}).get("ppio", {}).get("models", [])
    has_ppio_80b = any(
        isinstance(m, dict)
        and (m.get("name") == "qwen80binstruct" or "80b" in str(m.get("id", "")).lower())
        for m in ppio_models
    )
    primary_ok = primary == "qwen80binstruct" or primary in alias_refs
    if not primary_ok or not has_ppio_80b:
        print("[错误] 当前 multiap profile 未配置为 qwen80binstruct 默认模型。")
        print(f"当前 primary={primary!r}，qwen80binstruct refs={sorted(alias_refs)!r}")
        print("请运行：bash openclaw/setup.sh")
        sys.exit(1)


def _extract_fast_result(raw: str) -> dict | None:
    """从 OpenClaw JSON 输出中提取 run_fast_negotiation 工具结果。"""
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None

    def walk(obj):
        if isinstance(obj, dict):
            if "outcome" in obj and "transcript_turns" in obj:
                return obj
            for value in obj.values():
                found = walk(value)
                if found is not None:
                    return found
        elif isinstance(obj, list):
            for item in obj:
                found = walk(item)
                if found is not None:
                    return found
        return None

    found = walk(data)
    if found is not None:
        return found

    text = _collect_text(data)
    if not text:
        return None
    return _extract_result_from_text(text)


def _collect_text(obj) -> str:
    chunks: list[str] = []

    def walk(obj):
        if isinstance(obj, dict):
            for key in ("text", "finalAssistantVisibleText", "finalAssistantRawText"):
                value = obj.get(key)
                if isinstance(value, str):
                    chunks.append(value)
            for value in obj.values():
                walk(value)
        elif isinstance(obj, list):
            for item in obj:
                walk(item)

    walk(obj)
    return "\n".join(chunks)


def _extract_result_from_text(text: str) -> dict | None:
    candidates: list[str] = []
    parts = text.split("```")
    for i in range(1, len(parts), 2):
        block = parts[i].strip()
        if block.lower().startswith("json"):
            block = block[4:].strip()
        candidates.append(block)
    candidates.append(text)
    for candidate in candidates:
        try:
            obj = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and "outcome" in obj and "transcript_turns" in obj:
            return obj
    return None


if __name__ == "__main__":
    main()
