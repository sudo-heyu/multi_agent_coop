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
_NESTED_EDCA_KEYS = ("edca", "EDCA", "co_edca", "Co-EDCA", "coEDCA")
_NESTED_AC_ALIASES = {
    "BE": ("BE", "be", "AC_BE", "ac_be"),
    "VI": ("VI", "vi", "AC_VI", "ac_vi"),
}


def _canonical_proposal_key(key: object) -> object:
    """Canonicalize AP ids while preserving metadata keys such as _sr."""
    if not isinstance(key, str):
        return key
    lower = key.lower()
    return lower if lower in AP_IDS else key


def _lookup_nested_edca_value(params: dict, canonical: str):
    aliases = {
        "CWmin": ("CWmin", "cwmin"),
        "CWmax": ("CWmax", "cwmax"),
        "AIFSN": ("AIFSN", "aifsn"),
    }[canonical]
    for key in aliases:
        if key in params and params.get(key) is not None:
            return params.get(key)
    return None


def _extract_nested_edca_fields(params: dict) -> dict:
    fields = {
        key: value
        for key, value in params.items()
        if key in EDCA_KEYS
    }
    for ac, aliases in _NESTED_AC_ALIASES.items():
        group = None
        for alias in aliases:
            candidate = params.get(alias)
            if isinstance(candidate, dict):
                group = candidate
                break
        if group is None:
            continue
        for canonical in ("CWmin", "CWmax", "AIFSN"):
            value = _lookup_nested_edca_value(group, canonical)
            if value is not None:
                fields[f"{ac}_{canonical}"] = value
    return fields


def _normalize_proposal(proposal: dict) -> dict:
    """
    规范化提案为 AP 参数字典。

    模型有时把 Co-SR 提案写成扁平的 {"ap1": 6.0} 而非
    {"ap1": {"tx_power_dbm": 6.0}}。裸数值统一提升为 tx_power_dbm 嵌套形式，
    使策略推断与投票注入都能识别（否则会被误判为 co_edca）。

    模型也会自然写出 {"ap1": {"tx_power_dbm": 18, "edca": {...}}}。
    解析层把常见 nested EDCA 形态提升到顶层字段，后续验证器和执行器只面对
    一个统一参数协议。
    """
    normalized: dict = {}
    for ap_id, value in proposal.items():
        ap_key = _canonical_proposal_key(ap_id)
        if isinstance(value, bool):
            normalized[ap_key] = value
        elif isinstance(value, (int, float)):
            normalized[ap_key] = {"tx_power_dbm": float(value)}
        elif isinstance(value, dict):
            entry = dict(value)
            if isinstance(entry.get("concurrent_group"), list):
                entry["concurrent_group"] = [
                    str(item).lower() if str(item).lower() in AP_IDS else item
                    for item in entry["concurrent_group"]
                ]
            for nested_key in _NESTED_EDCA_KEYS:
                nested = entry.get(nested_key)
                if not isinstance(nested, dict):
                    continue
                nested_fields = _extract_nested_edca_fields(nested)
                if not nested_fields:
                    continue
                for key, nested_value in nested_fields.items():
                    entry.setdefault(key, nested_value)
                entry.pop(nested_key, None)
            normalized[ap_key] = entry
        else:
            normalized[ap_key] = value
    return normalized


def _infer_strategy_from_proposal(proposal: dict) -> str:
    """Detect negotiation strategy from fields present in the proposal JSON."""
    proposal = _normalize_proposal(proposal)
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
    Co-SR 提案必须先确定可用并发组。

    若模型已经显式给出 concurrent_group，则保留；若缺失，则使用确定性枚举器
    自动注入 best_group，避免退回“全网 AP 同时并发”的旧缺省语义。
    """
    proposal = _normalize_proposal(proposal)
    strategy = _infer_strategy_from_proposal(proposal)
    if strategy != "co_sr":
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
