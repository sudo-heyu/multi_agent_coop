"""Shared OpenClaw helpers for Multi-AP runs."""
from __future__ import annotations

import argparse
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_AP_CONFIG = REPO_ROOT / "ap_endpoints.json"


def _parse_executor_endpoints(raw: str) -> dict[str, str]:
    """
    Parse --ap-endpoints.

    Format:
      ap1=192.168.1.11:5002,ap2=192.168.1.12:5002,ap3=192.168.1.13:5002

    Values may include or omit the http:// prefix; the returned map always
    includes it.
    """
    endpoints: dict[str, str] = {}
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        if "=" not in item:
            raise argparse.ArgumentTypeError(
                f"--ap-endpoints 格式错误，期望 ap_id=host:port，实际收到 {item!r}"
            )
        ap_id, addr = item.split("=", 1)
        ap_id = ap_id.strip().lower()
        addr = addr.strip()
        if not addr.startswith("http"):
            addr = f"http://{addr}"
        endpoints[ap_id] = addr
    return endpoints
