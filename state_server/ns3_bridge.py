"""
Bridge ns-3 telemetry into the Multi-AP state server.

This script does not generate or modify QoS metrics. It only forwards telemetry
records produced by an ns-3 process and marks them as source="ns3".

Accepted input formats, one telemetry object per line:

1. ns-3 live output:
   TELEMETRY {"ap_id": "ap1", ...}

2. Single AP JSON record:
   {"ap_id": "ap1", "tx_power_dbm": 15, ...}

3. Snapshot JSON record:
   {"ap1": {...}, "ap2": {...}, "ap3": {...}}
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

AP_IDS = ("ap1", "ap2", "ap3")
DEFAULT_SERVER = "http://localhost:5001"


def _iter_lines(path: str, follow: bool):
    if path == "-":
        for line in sys.stdin:
            yield line
        return

    file_path = Path(path)
    with file_path.open(encoding="utf-8") as fh:
        while True:
            line = fh.readline()
            if line:
                yield line
                continue
            if not follow:
                break
            time.sleep(0.2)


def _records_from_obj(obj: dict) -> list[dict]:
    ap_id = str(obj.get("ap_id", "")).lower()
    if ap_id in AP_IDS:
        return [{**obj, "ap_id": ap_id}]

    records: list[dict] = []
    for candidate in AP_IDS:
        payload = obj.get(candidate)
        if isinstance(payload, dict):
            records.append({**payload, "ap_id": candidate})
    return records


def _parse_line(line: str) -> dict | None:
    if line.startswith("TELEMETRY "):
        line = line[len("TELEMETRY "):].strip()
    elif line.startswith("STA_TELEMETRY "):
        return None
    return json.loads(line)


def _normalize(record: dict) -> dict:
    payload = dict(record)
    payload["ap_id"] = str(payload["ap_id"]).lower()
    payload["source"] = "ns3"
    payload.setdefault("timestamp", datetime.now(timezone.utc).isoformat())
    return payload


def _post(session: requests.Session, server: str, payload: dict) -> bool:
    resp = session.post(f"{server.rstrip()}/state", json=payload, timeout=5)
    if resp.status_code == 200:
        print(f"[ns3] posted {payload['ap_id']} @ {payload['timestamp']}")
        return True
    print(f"[ns3] rejected {payload.get('ap_id')}: {resp.status_code} {resp.text}", file=sys.stderr)
    return False


def main() -> None:
    parser = argparse.ArgumentParser(description="Forward ns-3 telemetry JSONL to state_server /state")
    parser.add_argument("--input", "-i", default="-", help="JSONL file path, or '-' for stdin")
    parser.add_argument("--server", default=DEFAULT_SERVER, help=f"state server URL, default {DEFAULT_SERVER}")
    parser.add_argument("--follow", action="store_true", help="follow a growing JSONL file")
    args = parser.parse_args()

    session = requests.Session()
    session.trust_env = False
    ok = 0
    bad = 0
    for line in _iter_lines(args.input, args.follow):
        line = line.strip()
        if not line:
            continue
        try:
            obj = _parse_line(line)
        except json.JSONDecodeError as exc:
            bad += 1
            print(f"[ns3] invalid JSON: {exc}", file=sys.stderr)
            continue
        if obj is None:
            continue
        if not isinstance(obj, dict):
            bad += 1
            print("[ns3] ignored non-object JSON row", file=sys.stderr)
            continue
        records = _records_from_obj(obj)
        if not records:
            bad += 1
            print("[ns3] row has no ap_id and no ap1/ap2/ap3 snapshot", file=sys.stderr)
            continue
        for record in records:
            if _post(session, args.server, _normalize(record)):
                ok += 1
            else:
                bad += 1
    print(f"[ns3] done posted={ok} failed={bad}")


if __name__ == "__main__":
    main()
