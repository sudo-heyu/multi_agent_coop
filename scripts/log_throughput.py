"""
用户吞吐记录脚本

按固定间隔轮询状态服务器，把各 AP 的 throughput_mbps_user 连同时间戳
逐行写入 CSV 日志文件，供真实硬件实验后续分析/绘图。

用法：
  # 默认：轮询 http://localhost:5001，每 1s 记录一次，写入 logs/throughput/throughput_user_<启动时间>.csv
  python scripts/log_throughput.py

  # 指定服务器、间隔、输出文件
  python scripts/log_throughput.py --server http://192.168.1.100:5001 --interval 2 --out logs/exp1.csv

  # 只记录某个 AP
  python scripts/log_throughput.py --ap-id ap1

  # 限定记录时长（秒），到点自动停止；不指定则一直记录到 Ctrl-C
  python scripts/log_throughput.py --duration 300

CSV 格式：
  timestamp,elapsed_s,ap1_throughput_mbps_user,ap2_throughput_mbps_user,ap3_throughput_mbps_user
  其中 timestamp 为本地时间 ISO 8601；某 AP 数据缺失/过期时该列留空。
"""
import argparse
import csv
import sys
import time
from datetime import datetime
from pathlib import Path

import requests

DEFAULT_SERVER = "http://localhost:5001"
VALID_AP_IDS = ["ap1", "ap2", "ap3"]
FIELD = "throughput_mbps_user"


def _fetch_throughput(server_url: str, ap_ids: list[str]) -> dict[str, float | None]:
    """轮询一次 /state，返回 {ap_id: throughput_mbps_user 或 None}。

    数据缺失、过期（stale）或请求失败时该 AP 取 None。
    """
    result: dict[str, float | None] = {ap_id: None for ap_id in ap_ids}
    try:
        resp = requests.get(f"{server_url.rstrip('/')}/state", timeout=5)
        resp.raise_for_status()
        raw = resp.json()
    except requests.RequestException as exc:
        print(f"[警告] 状态服务器请求失败，本次记录为空：{exc}", file=sys.stderr)
        return result

    for ap_id in ap_ids:
        entry = raw.get(ap_id) or {}
        if entry.get("stale") or entry.get("data") is None:
            continue
        value = entry["data"].get(FIELD)
        if value is not None:
            result[ap_id] = float(value)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="记录各 AP 的用户吞吐（throughput_mbps_user）")
    parser.add_argument("--server", default=DEFAULT_SERVER,
                        help=f"状态服务器地址（默认 {DEFAULT_SERVER}）")
    parser.add_argument("--interval", type=float, default=1.0,
                        help="采样间隔秒数（默认 1）")
    parser.add_argument("--ap-id", choices=VALID_AP_IDS, default=None,
                        help="只记录指定 AP；不指定则记录全部三个")
    parser.add_argument("--duration", type=float, default=None,
                        help="记录时长（秒），到点自动停止；不指定则记录到 Ctrl-C")
    parser.add_argument("--out", default=None,
                        help="输出 CSV 路径；默认 logs/throughput/throughput_user_<启动时间>.csv")
    parser.add_argument("--append", action="store_true",
                        help="追加到已存在的文件（不写表头）")
    args = parser.parse_args()

    ap_ids = [args.ap_id] if args.ap_id else VALID_AP_IDS

    if args.out:
        out_path = Path(args.out)
    else:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        out_path = Path("logs") / "throughput" / f"throughput_user_{stamp}.csv"
    out_path.parent.mkdir(parents=True, exist_ok=True)

    write_header = not (args.append and out_path.exists())
    mode = "a" if args.append else "w"

    print(f"状态服务器：{args.server}")
    print(f"记录 AP：{', '.join(ap_ids)}")
    print(f"采样间隔：{args.interval:g}s" +
          (f"，时长：{args.duration:g}s" if args.duration else "，时长：直到 Ctrl-C"))
    print(f"输出文件：{out_path}")
    print("开始记录（Ctrl-C 停止）...\n")

    header = ["timestamp", "elapsed_s"] + [f"{ap}_{FIELD}" for ap in ap_ids]
    started = time.time()
    count = 0

    with out_path.open(mode, newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        if write_header:
            writer.writerow(header)
            f.flush()

        try:
            while True:
                loop_start = time.time()
                values = _fetch_throughput(args.server, ap_ids)
                ts = datetime.now().isoformat(timespec="milliseconds")
                elapsed = round(loop_start - started, 3)

                row = [ts, elapsed] + [
                    ("" if values[ap] is None else values[ap]) for ap in ap_ids
                ]
                writer.writerow(row)
                f.flush()  # 每行落盘，进程被中断也不丢已记录数据
                count += 1

                shown = "  ".join(
                    f"{ap}={'—' if values[ap] is None else f'{values[ap]:.2f}'}Mbps"
                    for ap in ap_ids
                )
                print(f"[{ts}] (+{elapsed:.1f}s) {shown}")

                if args.duration is not None and (time.time() - started) >= args.duration:
                    break

                sleep_s = args.interval - (time.time() - loop_start)
                if sleep_s > 0:
                    time.sleep(sleep_s)
        except KeyboardInterrupt:
            print("\n收到退出信号，停止记录。")

    print(f"\n共记录 {count} 行，已保存到 {out_path}")


if __name__ == "__main__":
    main()
