#!/usr/bin/env python3
"""
push_edca.py — 直接向香蕉派下发 EDCA 参数决策

用法：
  python push_edca.py <cwmin> <cwmax> <aifsn>           # 三个 AP 使用相同参数
  python push_edca.py 3 15 3                            # 全部统一
  python push_edca.py --ap1 3 15 3 --ap2 7 31 2        # 分 AP 指定
  python push_edca.py --ap1 3 15 3                      # 只下发 ap1
  python push_edca.py 3 15 3 --dry-run                  # 预览 payload，不发送

参数范围（IEEE 802.11）：
  CWmin:  3 – 1023
  CWmax:  7 – 1023（必须 > CWmin）
  AIFSN:  1 – 15
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

CWMIN_RANGE = (3, 1023)
CWMAX_RANGE = (7, 1023)
AIFSN_RANGE = (1, 15)

# ── 参数解析 ──────────────────────────────────────────────────────────────────

class EdcaParams(argparse.Action):
    """将 nargs=3 的字符串列表转换为 (cwmin, cwmax, aifsn) 元组。"""
    def __call__(self, parser, namespace, values, option_string=None):
        setattr(namespace, self.dest, tuple(int(v) for v in values))


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="直接向香蕉派 /apply 接口下发 EDCA 参数决策",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )

    # 全局位置参数：cwmin cwmax aifsn（三个 AP 统一）
    p.add_argument("global_params", nargs="*", metavar="PARAM",
                   help="全局 EDCA 参数（顺序：cwmin cwmax aifsn），三个 AP 统一使用")

    # 分 AP 指定
    for ap in ("ap1", "ap2", "ap3"):
        p.add_argument(f"--{ap}", nargs=3, metavar=("CWMIN", "CWMAX", "AIFSN"),
                       action=EdcaParams,
                       help=f"{ap} 的 EDCA 参数：cwmin cwmax aifsn")

    p.add_argument("--strategy", default="co_edca",
                   choices=["co_edca", "joint"],
                   help="策略标签（默认: co_edca）")
    p.add_argument("--session-id", default=None,
                   help="会话 ID（默认自动生成）")
    p.add_argument("--timeout", type=float, default=8.0, help="请求超时秒数（默认: 8）")
    p.add_argument("--dry-run", action="store_true", help="只打印 payload，不发送请求")

    args = p.parse_args()

    if args.global_params and len(args.global_params) != 3:
        p.error("全局参数必须同时提供 cwmin cwmax aifsn 三个值")

    return args


def resolve_params(args: argparse.Namespace) -> dict[str, tuple[int, int, int]]:
    """解析每个 AP 的目标 EDCA 参数。返回 {ap_id: (cwmin, cwmax, aifsn)}。"""
    ap_ids = list(ENDPOINTS.keys())

    # --ap1/--ap2/--ap3 优先级最高
    per_ap = {ap: getattr(args, ap) for ap in ap_ids if getattr(args, ap) is not None}
    if per_ap:
        return per_ap

    # 全局位置参数
    if args.global_params:
        triple = tuple(int(v) for v in args.global_params)
        return {ap: triple for ap in ap_ids}

    # 无参数
    return {}


# ── 校验 ──────────────────────────────────────────────────────────────────────

def validate_edca(ap_id: str, cwmin: int, cwmax: int, aifsn: int) -> None:
    errors = []
    if not (CWMIN_RANGE[0] <= cwmin <= CWMIN_RANGE[1]):
        errors.append(f"CWmin={cwmin} 超出范围 {CWMIN_RANGE}")
    if not (CWMAX_RANGE[0] <= cwmax <= CWMAX_RANGE[1]):
        errors.append(f"CWmax={cwmax} 超出范围 {CWMAX_RANGE}")
    if not (AIFSN_RANGE[0] <= aifsn <= AIFSN_RANGE[1]):
        errors.append(f"AIFSN={aifsn} 超出范围 {AIFSN_RANGE}")
    if cwmax <= cwmin:
        errors.append(f"CWmax={cwmax} 必须大于 CWmin={cwmin}")
    if errors:
        for e in errors:
            print(f"[错误] {ap_id}: {e}", file=sys.stderr)
        sys.exit(1)


# ── 下发逻辑 ──────────────────────────────────────────────────────────────────

def build_payload(ap_id: str, cwmin: int, cwmax: int, aifsn: int,
                  strategy: str, session_id: str) -> dict:
    return {
        "session_id": session_id,
        "strategy":   strategy,
        "ap_id":      ap_id,
        "params": {
            "CWmin": cwmin,
            "CWmax": cwmax,
            "AIFSN": aifsn,
        },
    }


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


# ── 主流程 ────────────────────────────────────────────────────────────────────

def main() -> None:
    args = parse_args()
    target = resolve_params(args)

    if not target:
        print("未指定任何参数，退出。", file=sys.stderr)
        print("示例：python push_edca.py 3 15 3", file=sys.stderr)
        sys.exit(1)

    for ap_id, (cwmin, cwmax, aifsn) in target.items():
        validate_edca(ap_id, cwmin, cwmax, aifsn)

    session_id = args.session_id or datetime.now(timezone.utc).strftime("manual_%Y%m%d_%H%M%S")

    print(f"{'[DRY-RUN] ' if args.dry_run else ''}策略={args.strategy}  会话={session_id}")
    for ap_id, (cwmin, cwmax, aifsn) in target.items():
        print(f"  {ap_id}: CWmin={cwmin}  CWmax={cwmax}  AIFSN={aifsn}")
    print()

    payloads = {
        ap_id: build_payload(ap_id, *triple, args.strategy, session_id)
        for ap_id, triple in target.items()
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
            cwmin, cwmax, aifsn = target[ap_id]
            mark = "✓" if ok else "✗"
            print(f"[{mark}] {ap_id.upper()} CWmin={cwmin} CWmax={cwmax} AIFSN={aifsn}  →  {msg}")
            if ok:
                ok_count += 1

    print(f"\n完成：{ok_count}/{len(payloads)} 个 AP 成功。")
    if ok_count < len(payloads):
        sys.exit(1)


if __name__ == "__main__":
    main()
