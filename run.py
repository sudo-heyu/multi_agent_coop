"""
多 AP 协商系统触发脚本

用法：
  python run.py                            # 从状态服务器获取数据（默认模型 qwen3:14b）
  python run.py --mock                     # 使用 mock 数据（联合场景，触发 Co-SR + Co-EDCA）
  python run.py --mock --scene sr          # 仅 Co-SR 场景
  python run.py --mock --scene edca        # 仅 Co-EDCA 场景
  python run.py qwen3:14b --mock           # 指定模型 + mock 数据
  python run.py --server http://192.168.1.100:5001  # 指定服务器地址
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from src.logger import SessionLogger
from src.orchestrator import NegotiationOrchestrator
from src.skills.state import get_all_states, StateStaleError

# ------------------------------------------------------------------
# Mock 数据：三个预设场景
# ------------------------------------------------------------------

# 场景一：Co-SR（三个 AP 功率不对称，AP1 高位干扰邻居，EDCA 正常）
# AP1=20dBm(高), AP2=14dBm(中), AP3=8dBm(低)
# neighbor_rssi 反映各 AP 在其当前功率下被邻居接收到的信号强度（路径损耗 dB 线性）
# AP2 接收到 AP1 的 -69 dBm，触发 Co-SR；AP3 距 AP1 较远，受影响较小
# STA 紧靠本 AP（sta_rssi ≈ -45~-50 dBm），降功率后不会断连
MOCK_SCENE_SR = {
    "ap1": {
        "tx_power_dbm": 20.0,
        "cwmin": 7, "cwmax": 15, "aifsn": 2,
        "channel_busy_ratio": 0.45,
        "tx_retries_ratio": 0.08,
        # AP1 处接收：AP2 在 14dBm(-69 + (14-20)=-75)，AP3 在 8dBm(-76 + (8-20)=-88)
        "neighbor_rssi_dbm": {"ap2": -75.0, "ap3": -88.0},
        "sta_rssi_dbm": -45.0,
        "noise_floor_dbm": -92.0,
        "throughput_mbps": 22.1,
        "latency_ms": 210.0,
        "packet_loss_pct": 0.5,
    },
    "ap2": {
        "tx_power_dbm": 14.0,
        "cwmin": 7, "cwmax": 15, "aifsn": 2,
        "channel_busy_ratio": 0.50,
        "tx_retries_ratio": 0.10,
        # AP2 处接收：AP1 在 20dBm(-69)，AP3 在 8dBm(-70 + (8-20)=-82)
        "neighbor_rssi_dbm": {"ap1": -69.0, "ap3": -82.0},
        "sta_rssi_dbm": -48.0,
        "noise_floor_dbm": -91.0,
        "throughput_mbps": 20.3,
        "latency_ms": 195.0,
        "packet_loss_pct": 0.3,
    },
    "ap3": {
        "tx_power_dbm": 8.0,
        "cwmin": 7, "cwmax": 15, "aifsn": 2,
        "channel_busy_ratio": 0.38,
        "tx_retries_ratio": 0.06,
        # AP3 处接收：AP1 在 20dBm(-76)，AP2 在 14dBm(-70 + (14-20)=-76)
        "neighbor_rssi_dbm": {"ap1": -76.0, "ap2": -76.0},
        "sta_rssi_dbm": -50.0,
        "noise_floor_dbm": -90.0,
        "throughput_mbps": 28.5,
        "latency_ms": 120.0,
        "packet_loss_pct": 0.1,
    },
}

# 场景二：Co-EDCA（EDCA 拥塞严重，邻居 RSSI 弱）
MOCK_SCENE_EDCA = {
    "ap1": {
        "tx_power_dbm": 10.0,
        "cwmin": 3, "cwmax": 7, "aifsn": 1,
        "channel_busy_ratio": 0.82,
        "tx_retries_ratio": 0.31,
        "neighbor_rssi_dbm": {"ap2": -85.0, "ap3": -88.0},
        "sta_rssi_dbm": -55.0,
        "noise_floor_dbm": -92.0,
        "throughput_mbps": 18.4,
        "latency_ms": 312.0,
        "packet_loss_pct": 1.2,
    },
    "ap2": {
        "tx_power_dbm": 10.0,
        "cwmin": 7, "cwmax": 15, "aifsn": 2,
        "channel_busy_ratio": 0.55,
        "tx_retries_ratio": 0.12,
        "neighbor_rssi_dbm": {"ap1": -85.0, "ap3": -87.0},
        "sta_rssi_dbm": -61.0,
        "noise_floor_dbm": -91.0,
        "throughput_mbps": 28.7,
        "latency_ms": 185.0,
        "packet_loss_pct": 0.4,
    },
    "ap3": {
        "tx_power_dbm": 10.0,
        "cwmin": 15, "cwmax": 63, "aifsn": 4,
        "channel_busy_ratio": 0.38,
        "tx_retries_ratio": 0.05,
        "neighbor_rssi_dbm": {"ap1": -88.0, "ap2": -87.0},
        "sta_rssi_dbm": -58.0,
        "noise_floor_dbm": -90.0,
        "throughput_mbps": 34.1,
        "latency_ms": 98.0,
        "packet_loss_pct": 0.1,
    },
}

# 场景三：联合（高功率 + 严重拥塞，同时触发 Co-SR 和 Co-EDCA）
# neighbor_rssi 与场景一一致（STA 距本 AP 近，降功率不断连）
MOCK_SCENE_JOINT = {
    "ap1": {
        "tx_power_dbm": 20.0,
        "cwmin": 3, "cwmax": 7, "aifsn": 1,
        "channel_busy_ratio": 0.82,
        "tx_retries_ratio": 0.31,
        "neighbor_rssi_dbm": {"ap2": -69.0, "ap3": -76.0},
        "sta_rssi_dbm": -45.0,
        "noise_floor_dbm": -92.0,
        "throughput_mbps": 18.4,
        "latency_ms": 312.0,
        "packet_loss_pct": 1.2,
    },
    "ap2": {
        "tx_power_dbm": 20.0,
        "cwmin": 7, "cwmax": 15, "aifsn": 2,
        "channel_busy_ratio": 0.55,
        "tx_retries_ratio": 0.12,
        "neighbor_rssi_dbm": {"ap1": -69.0, "ap3": -70.0},
        "sta_rssi_dbm": -48.0,
        "noise_floor_dbm": -91.0,
        "throughput_mbps": 28.7,
        "latency_ms": 185.0,
        "packet_loss_pct": 0.4,
    },
    "ap3": {
        "tx_power_dbm": 20.0,
        "cwmin": 15, "cwmax": 63, "aifsn": 4,
        "channel_busy_ratio": 0.38,
        "tx_retries_ratio": 0.05,
        "neighbor_rssi_dbm": {"ap1": -76.0, "ap2": -70.0},
        "sta_rssi_dbm": -50.0,
        "noise_floor_dbm": -90.0,
        "throughput_mbps": 34.1,
        "latency_ms": 98.0,
        "packet_loss_pct": 0.1,
    },
}

MOCK_SCENES = {
    "sr":    MOCK_SCENE_SR,
    "edca":  MOCK_SCENE_EDCA,
    "joint": MOCK_SCENE_JOINT,
}


def main():
    parser = argparse.ArgumentParser(description="多 AP 协商系统")
    parser.add_argument("model", nargs="?", default="qwen3:14b",
                        help="Ollama 模型名（默认 qwen3:14b）")
    parser.add_argument("--mock", action="store_true",
                        help="使用 mock 数据，无需启动状态服务器")
    parser.add_argument("--scene", choices=["sr", "edca", "joint"], default="joint",
                        help="mock 场景：sr / edca / joint（默认 joint）")
    parser.add_argument("--server", default="http://localhost:5001",
                        help="状态服务器地址（默认 http://localhost:5001）")
    args = parser.parse_args()

    scene_labels = {
        "sr":    "场景一：静态 Co-SR 仿真",
        "edca":  "场景二：静态 Co-EDCA 仿真",
        "joint": "场景三：静态 Co-SR + Co-EDCA 联合仿真",
    }
    print("=" * 60)
    print(f"多 AP 协商系统 — {scene_labels.get(args.scene, args.scene)}")
    print(f"模型：{args.model}")
    print(f"数据来源：{'mock(' + args.scene + ')' if args.mock else args.server}")
    print("=" * 60)

    # 获取 AP 状态
    if args.mock:
        ap_state = MOCK_SCENES[args.scene]
    else:
        try:
            ap_state = get_all_states(args.server)
            print(f"已从 {args.server} 获取三台 AP 的最新状态。")
        except (ConnectionError, StateStaleError) as e:
            print(f"\n[错误] {e}")
            print("提示：使用 --mock 参数可跳过服务器，直接以 mock 数据运行。")
            sys.exit(1)

    scene = args.scene if args.mock else "live"
    logger = SessionLogger()
    logger.session_start(model=args.model, scene=scene, ap_state=ap_state)

    agents_dir = Path(__file__).parent / "agents"
    orchestrator = NegotiationOrchestrator(
        agents_dir=agents_dir,
        model=args.model,
        logger=logger,
    )
    try:
        orchestrator.run(ap_state)
    except Exception as e:
        logger.session_end(outcome="error", total_rounds=0)
        raise


if __name__ == "__main__":
    main()
