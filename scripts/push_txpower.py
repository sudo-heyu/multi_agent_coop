#!/usr/bin/env python3
"""
push_txpower.py — 直接向香蕉派下发 TxPower 决策

用法：
  python push_txpower.py 16 16 16                 # 位置参数：ap1 ap2 ap3
  python push_txpower.py 18                       # 只下发 ap1，其余跳过
  python push_txpower.py 18 15 20                 # 分别指定
  python push_txpower.py                          # 使用默认功率（全部 20 dBm）
  python push_txpower.py 16 16 16 --dry-run       # 只打印 payload，不发请求
  python push_txpower.py --all 17                 # 三个 AP 统一设置
"""

import argparse
import json
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

try:
    import requests
except ImportError:
    print("缺少 requests 库：pip install requests", file=sys.stderr)
    sys.exit(1)

# ── 配置 ─────────────────────────────────────────────────────────────────────

ENDPOINTS: dict[str, str] = {
    "ap1": "http://192.168.1.1:5002",
    "ap2": "http://192.168.1.2:5002",
    "ap3": "http://192.168.1.3:5002",
}

TX_POWER_MIN = 1
TX_POWER_MAX = 23
DEFAULT_TX_POWER = 20

# ── 解析参数 ──────────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="直接向香蕉派 /apply 接口下发 TxPower 决策",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("powers", type=float, nargs="*", metavar="DBM",
                   help="按 ap1 ap2 ap3 顺序传入功率（dBm），可省略尾部 AP")
    p.add_argument("--all", type=float, metavar="DBM", dest="all_ap",
                   help="三个 AP 统一设置相同功率")
    p.add_argument("--strategy", default="co_sr",
                   choices=["co_sr"],
                   help="策略标签（默认: co_sr）")
    p.add_argument("--session-id", default=None,
                   help="会话 ID（默认自动生成）")
    p.add_argument("--timeout", type=float, default=8.0, help="请求超时秒数（默认: 8）")
    p.add_argument("--dry-run", action="store_true", help="只打印 payload，不发送请求")

    args = p.parse_args()
    if len(args.powers) > 3:
        p.error(f"最多接受 3 个位置参数（ap1 ap2 ap3），收到 {len(args.powers)} 个")
    return args


def resolve_powers(args: argparse.Namespace) -> dict[str, float]:
    """根据命令行参数解析每个 AP 的目标功率。"""
    ap_ids = list(ENDPOINTS.keys())  # ["ap1", "ap2", "ap3"]

    if args.all_ap is not None:
        return {ap: args.all_ap for ap in ap_ids}

    if args.powers:
        return {ap_ids[i]: dbm for i, dbm in enumerate(args.powers)}

    # 无参数时全部使用默认值
    return {ap: DEFAULT_TX_POWER for ap in ap_ids}


def validate_power(ap_id: str, dbm: float) -> None:
    if not (TX_POWER_MIN <= dbm <= TX_POWER_MAX):
        print(f"[错误] {ap_id} TxPower={dbm} dBm 超出合法范围 "
              f"[{TX_POWER_MIN}, {TX_POWER_MAX}]，退出。", file=sys.stderr)
        sys.exit(1)


# ── 下发逻辑 ──────────────────────────────────────────────────────────────────

def send_apply(ap_id: str, url: str, payload: dict, timeout: float) -> tuple[bool, str]:
    try:
        r = requests.post(f"{url.rstrip('/')}/apply", json=payload, timeout=timeout)
        if r.status_code == 200:
            detail = r.json().get("details", r.text)
            return True, str(detail)
        return False, f"HTTP {r.status_code}: {r.text[:200]}"
    except requests.exceptions.ConnectionError:
        return False, "连接失败（AP 不可达）"
    except requests.exceptions.Timeout:
        return False, f"请求超时（>{timeout}s）"
    except Exception as exc:
        return False, str(exc)


def build_payload(ap_id: str, dbm: float, strategy: str, session_id: str) -> dict:
    return {
        "session_id": session_id,
        "strategy":   strategy,
        "ap_id":      ap_id,
        "params": {
            "tx_power_dbm": int(dbm) if dbm == int(dbm) else dbm,
        },
    }


# ── 主流程 ────────────────────────────────────────────────────────────────────

def main() -> None:
    args = parse_args()
    powers = resolve_powers(args)

    for ap_id, dbm in powers.items():
        validate_power(ap_id, dbm)

    session_id = args.session_id or datetime.now(timezone.utc).strftime("manual_%Y%m%d_%H%M%S")

    print(f"{'[DRY-RUN] ' if args.dry_run else ''}策略={args.strategy}  会话={session_id}")
    print(f"目标功率: { {ap: f'{dbm} dBm' for ap, dbm in powers.items()} }\n")

    payloads = {
        ap_id: build_payload(ap_id, dbm, args.strategy, session_id)
        for ap_id, dbm in powers.items()
    }

    if args.dry_run:
        for ap_id, payload in payloads.items():
            print(f"── {ap_id.upper()} → {ENDPOINTS[ap_id]}/apply")
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        return

    ok_count = 0
    with ThreadPoolExecutor(max_workers=len(payloads)) as pool:
        futures = {
            pool.submit(send_apply, ap_id, ENDPOINTS[ap_id], payload, args.timeout): ap_id
            for ap_id, payload in payloads.items()
        }
        for future in as_completed(futures):
            ap_id = futures[future]
            ok, msg = future.result()
            mark = "✓" if ok else "✗"
            dbm = powers[ap_id]
            print(f"[{mark}] {ap_id.upper()} TxPower={dbm} dBm  →  {msg}")
            if ok:
                ok_count += 1

    print(f"\n完成：{ok_count}/{len(payloads)} 个 AP 成功。")
    if ok_count < len(payloads):
        sys.exit(1)


if __name__ == "__main__":
    main()
