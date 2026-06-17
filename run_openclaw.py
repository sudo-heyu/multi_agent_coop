"""
纯 OpenClaw 架构（C：无协调者）入口 —— 接力总线驱动三台 AP 自驱动协商。

用法：
  python run_openclaw.py --scene joint            # mock A：预设场景
  python run_openclaw.py --scene sr --max-steps 20
  python run_openclaw.py --scene edca --no-feeder  # 不喂曲线，仅跑协商

前置：openclaw 已装、ollama 运行、已执行过 `bash openclaw/setup.sh`。
"""
import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent / "openclaw" / "mcp"))

import requests

from run import MOCK_SCENES, start_mock_server
from state_server.mock_feeder import MockTelemetryFeeder
import orchestration as orch


def _print_event(phase, who, reply):
    print(f"\n{'═'*70}\n[{phase}] {who.upper()}")
    print(reply.strip())


def main():
    ap = argparse.ArgumentParser(description="多 AP 协商（纯 OpenClaw / 无协调者）")
    ap.add_argument("--scene", choices=["sr", "edca", "joint"], default="joint")
    ap.add_argument("--server", default="http://localhost:5001")
    ap.add_argument("--max-steps", type=int, default=24)
    ap.add_argument("--no-feeder", action="store_true", help="不启动曲线喂数器")
    args = ap.parse_args()

    scene = MOCK_SCENES[args.scene]
    print(f"[run_openclaw] scene={args.scene} server={args.server} max_steps={args.max_steps}")

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
    result = orch.structured_relay(on_event=_print_event)
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


if __name__ == "__main__":
    main()
