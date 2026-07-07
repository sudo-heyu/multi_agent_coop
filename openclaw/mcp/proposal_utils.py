"""
提案 / JSON / 策略推断 的纯函数工具。

从原 src/orchestrator.py 模块级 helper 原样迁移而来（逻辑零改写），
作为纯 OpenClaw 架构自包含的解析层,供 orchestration.py 使用。
这些函数无状态,只依赖保留为基础设施的 src/tools/sr.py。
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.tools import sr as _sr

AP_IDS = ["ap1", "ap2", "ap3"]
EDCA_KEYS = {
    "CWmin", "CWmax", "AIFSN", "cwmin", "cwmax", "aifsn",
    "BE_CWmin", "BE_CWmax", "BE_AIFSN", "be_cwmin", "be_cwmax", "be_aifsn",
    "VI_CWmin", "VI_CWmax", "VI_AIFSN", "vi_cwmin", "vi_cwmax", "vi_aifsn",
}


def _normalize_proposal(proposal: dict) -> dict:
    """
    规范化提案为嵌套格式。

    模型有时把 Co-SR 提案写成扁平的 {"ap1": 6.0} 而非
    {"ap1": {"tx_power_dbm": 6.0}}。裸数值统一提升为 tx_power_dbm 嵌套形式，
    使策略推断与投票注入都能识别（否则会被误判为 co_edca）。
    """
    normalized: dict = {}
    for ap_id, value in proposal.items():
        if isinstance(value, bool):
            normalized[ap_id] = value
        elif isinstance(value, (int, float)):
            normalized[ap_id] = {"tx_power_dbm": float(value)}
        else:
            normalized[ap_id] = value
    return normalized


def _infer_strategy_from_proposal(proposal: dict) -> str:
    """Detect negotiation strategy from fields present in the proposal JSON."""
    has_sr = any(
        (isinstance(v, dict) and ("tx_power_dbm" in v or "obss_pd_dbm" in v))
        or isinstance(v, (int, float))
        for v in proposal.values()
        if not isinstance(v, bool)
    )
    has_edca = any(
        isinstance(v, dict) and any(k in v for k in EDCA_KEYS)
        for v in proposal.values()
    )
    if has_sr and has_edca:
        return "joint"
    if has_sr:
        return "co_sr"
    if has_edca:
        return "co_edca"
    return "co_edca"


def _sr_concurrent_group_from_proposal(proposal: dict | None) -> list[str]:
    if not isinstance(proposal, dict):
        return []
    meta = proposal.get("_sr") or proposal.get("sr") or {}
    if isinstance(meta, dict):
        group = meta.get("concurrent_group") or meta.get("concurrent_aps")
        if isinstance(group, list):
            return [str(ap).lower() for ap in group]
    group = proposal.get("concurrent_group")
    if isinstance(group, list):
        return [str(ap).lower() for ap in group]
    return []


def _with_sr_concurrent_group(proposal: dict, ap_state: dict) -> dict:
    """
    Co-SR / joint 提案必须先确定可用并发组。

    若模型已经显式给出 concurrent_group，则保留；若缺失，则使用确定性枚举器
    自动注入 best_group，避免退回“全网 AP 同时并发”的旧缺省语义。
    """
    strategy = _infer_strategy_from_proposal(proposal)
    if strategy not in ("co_sr", "joint"):
        return proposal

    groups = _sr.select_concurrent_groups(ap_state)
    best = groups.get("best_group") if isinstance(groups, dict) else None
    existing_group = _sr_concurrent_group_from_proposal(proposal)
    if existing_group or not best:
        return proposal

    updated = dict(proposal)
    sr_meta = dict(updated.get("_sr") or {})
    sr_meta["concurrent_group"] = list(best.get("concurrent_group", []))
    sr_meta["non_concurrent_aps"] = list(best.get("non_concurrent_aps", []))
    sr_meta["source"] = "orchestrator_auto_select"
    updated["_sr"] = sr_meta
    return updated


def _extract_json(text: str) -> dict | None:
    """从 agent 回复中提取第一个合法 JSON 对象。"""
    for m in re.finditer(r"```(?:json)?\s*(.*?)\s*```", text, re.DOTALL):
        candidate = m.group(1).strip()
        try:
            parsed = json.loads(candidate)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass

    decoder = json.JSONDecoder()
    for match in re.finditer(r"\{", text):
        try:
            parsed, _ = decoder.raw_decode(text[match.start():])
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass
    return None


def _extract_proposal(text: str) -> dict | None:
    """扫描文本中所有 JSON 块，返回第一个含 ap1/ap2/ap3 键的对象。"""
    def _matches(d: dict) -> dict | None:
        ap_keys = {k.lower() for k in d}
        if set(AP_IDS).issubset(ap_keys):
            return d
        for key in ("proposal", "final_proposal", "decision", "params"):
            nested = d.get(key)
            if isinstance(nested, dict) and set(AP_IDS).issubset({k.lower() for k in nested}):
                return nested
        return None

    for m in re.finditer(r"```(?:json)?\s*(.*?)\s*```", text, re.DOTALL):
        try:
            parsed = json.loads(m.group(1).strip())
            if isinstance(parsed, dict):
                hit = _matches(parsed)
                if hit is not None:
                    return _normalize_proposal(hit)
        except json.JSONDecodeError:
            pass

    decoder = json.JSONDecoder()
    for match in re.finditer(r"\{", text):
        try:
            parsed, _ = decoder.raw_decode(text[match.start():])
            if isinstance(parsed, dict):
                hit = _matches(parsed)
                if hit is not None:
                    return _normalize_proposal(hit)
        except json.JSONDecodeError:
            pass
    return None


def _json(data: dict) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2)
