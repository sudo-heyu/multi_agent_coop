"""用项目真实记忆代码，跑多轮"完整场景协商"的确定性替身，检验收敛闭环。

背景：沙箱内没有 ns-3 二进制、没有 openclaw CLI 可达的模型后端（PPIO 出口被
代理拒绝，也没有本机 ollama），无法按 README 走真实 ns-3 + LLM 协商路径。
这里改用项目自带的确定性合成环境（与 tests/test_goal_benchmark.py 同源），
但不只跑 assert，而是把每一轮的 episode/outcome/目标状态/语义规律都打印出来，
直接观察「协商 -> 案例 -> 效果评估 -> 目标归因 -> 规律归纳 -> 下一轮检索注入」
这条闭环在多轮之后到底收不收敛。
"""
from __future__ import annotations

import copy
import json
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

os.environ.setdefault("MULTIAP_MEMORY_LLM", "0")

import sys
sys.path.insert(0, "/sessions/beautiful-admiring-archimedes/mnt/multi_agent_coop")

from tests.mock_scenes import MOCK_SCENES
from src.memory import materialize_episode, find_similar_episodes, find_episode_memory
from src.memory.goals import (
    create_goal, refresh_goal_after_evaluation, register_attempt,
    record_attempt_result,
)
from src.memory.outcome import apply_evaluation_to_episode, evaluate_deltas, classify
from src.memory.semantic import induce_rules, find_matching_rules, format_rule
from src.persistence import EventStore

TARGET = {"ap": "ap2", "metric": "tx_retries_ratio", "op": "<=", "value": 0.15}
BASE_RETRIES = 0.35
INITIAL_CWMIN = 31


def env_retries(cwmin: int, k: float) -> float:
    return round(max(0.02, BASE_RETRIES - k * (INITIAL_CWMIN - cwmin)), 6)


def next_cwmin(previous: int, classification: str | None) -> int:
    if classification == "worsened":
        return min(INITIAL_CWMIN, previous * 2 + 1)
    if classification == "no_effect":
        return max(1, previous - 4)
    return max(1, previous // 2)


def episode(store, run_id, decision, scene="edca"):
    state = copy.deepcopy(MOCK_SCENES[scene])
    store.start_run(run_id, mode="mock", scene=scene, model="openclaw")
    store.append_event(run_id, "session_start",
                        {"model": "openclaw", "scene": scene, "ap_state": state})
    store.record_snapshot(run_id, label="initial", source="s", state=state)
    store.append_event(run_id, "final_decision", {"decision": decision, "raw_response": "{}"})
    store.append_event(run_id, "validation_result",
                        {"approved": True, "strategy": "co_edca", "summary": "ok"})
    store.append_event(run_id, "session_end", {"outcome": "success", "total_rounds": 1})
    store.complete_run(run_id, "success")
    return materialize_episode(store, run_id), state


def observed_state_for(baseline_state, retries):
    """把 tx_retries_ratio 的变化按简单模型映射到 classify() 真正使用的
    throughput/latency/packet_loss 指标，这样 outcome 评估才有真实信号，
    而不是像单元测试里那样直接硬编码 verdict。"""
    improvement = max(0.0, min(1.0, (BASE_RETRIES - retries) / BASE_RETRIES))
    observed = copy.deepcopy(baseline_state)
    row = observed["ap2"]
    base = baseline_state["ap2"]
    row["tx_retries_ratio"] = retries
    row["latency_ms"] = round(base["latency_ms"] * (1 - 0.5 * improvement), 2)
    row["throughput_mbps_iperf"] = round(base["throughput_mbps_iperf"] * (1 + 0.3 * improvement), 2)
    row["packet_loss_pct"] = round(base["packet_loss_pct"] * (1 - 0.5 * improvement), 4)
    return observed


def window_and_apply(store, run_id, baseline_state, retries):
    """用真实 evaluate_deltas/classify 产出 verdict，再走 apply_evaluation_to_episode
    完整闭环（回写 episode 质量分 + 目标进度 + 语义规律证据池）。"""
    observed = observed_state_for(baseline_state, retries)
    deltas = evaluate_deltas(baseline_state, observed)
    verdict, confidence = classify(deltas)
    due = (datetime.now(timezone.utc) - timedelta(seconds=60)).isoformat()
    for label in ("w1", "w2"):
        record, _ = store.schedule_evaluation(
            run_id, window_label=label, window_seconds=10.0, due_at=due,
            baseline=baseline_state,
        )
        store.finish_evaluation(
            record["evaluation_id"], status="collected",
            observed=observed, deltas=deltas, verdict=verdict, confidence=confidence,
        )
    return apply_evaluation_to_episode(store, run_id), verdict


def drive_goal_chain(store, k, chain_label):
    """跑一条目标驱动多轮协商链直到 achieved 或 blocked，打印每次 attempt。"""
    goal = create_goal(store, target=TARGET, budget_attempts=5)
    cwmin = 15
    attempt_index = 0
    print(f"\n--- 链路 {chain_label}（敏感度 k={k:.4f}）goal_id={goal['goal_id']} ---")
    while store.get_goal(goal["goal_id"])["status"] == "active":
        attempt_index += 1
        run_id = f"{chain_label}-run-{attempt_index}"
        attempt = register_attempt(store, goal["goal_id"], run_id)
        classification = (attempt.get("attribution") or {}).get("classification")
        if attempt_index > 1:
            cwmin = next_cwmin(cwmin, classification)
        ep, state = episode(store, run_id, {"ap2": {"CWmin": cwmin}})
        record_attempt_result(store, run_id, outcome="success")
        observed_retries = env_retries(cwmin, k)
        summary, verdict = window_and_apply(store, run_id, state, observed_retries)
        goal_progress = (summary or {}).get("goal_progress")
        print(f"  attempt#{attempt_index} run={run_id} cwmin={cwmin:>3} "
              f"归因={classification!s:<28} 实际retries={observed_retries:.4f} "
              f"outcome_verdict={verdict:<10} -> goal_progress={goal_progress}")
    final_status = store.get_goal(goal["goal_id"])["status"]
    print(f"  => 链路终态: {final_status}（共 {attempt_index} 次尝试，预算 5）")
    return final_status, attempt_index


def _run(store):
    results = []
    # 5 条不同敏感度的目标驱动协商链，模拟"跑几遍完整场景协商"
    sensitivities = [0.018, 0.012, 0.008, 0.006, 0.003]
    for i, k in enumerate(sensitivities, start=1):
        status, attempts = drive_goal_chain(store, k, f"chain{i}")
        results.append((k, status, attempts))

    print("\n=== 汇总：5 条目标驱动协商链的收敛情况 ===")
    for k, status, attempts in results:
        print(f"  k={k:.4f}  终态={status:<10} 尝试次数={attempts}")
    achieved = sum(1 for _, s, _ in results if s == "achieved")
    print(f"  达标 {achieved}/{len(results)} 条链")

    print("\n=== 语义规律归纳（跨案例） ===")
    rules = induce_rules(store, min_support=2, use_llm=False)
    if not rules:
        print("  未归纳出规律（support 不足或全部 inconclusive）")
    for rule in rules:
        print(f"  {format_rule(rule)}")

    print("\n=== 下一次同拓扑协商：能否检索到历史案例 + 规律注入提案？ ===")
    probe_state = copy.deepcopy(MOCK_SCENES["edca"])
    probe_state["ap2"]["CWmin"] = 15
    similar = find_similar_episodes(store, probe_state, limit=5, require_evaluation=True)
    print(f"  find_similar_episodes 命中 {len(similar)} 条已评估历史案例")
    for item in similar[:5]:
        verdict = (item.get("evaluation") or {}).get("final_verdict")
        print(f"    run={item['run_id']} verdict={verdict} "
              f"quality={item['quality_score']} retrieval_score={item['retrieval_score']}")
    mem = find_episode_memory(store, probe_state)
    print(f"  正向案例 positive={len(mem['positive'])}，失败警告 warnings={len(mem['warnings'])}")

    matched_rules = find_matching_rules(
        store, probe_state, scene="edca", min_confidence=0.5,
        actionable_min_support=2,
    )
    print(f"  find_matching_rules 命中 {len(matched_rules)} 条可注入规律"
          f"（actionable_min_support 降到 2 便于在小样本下观察，"
          f"线上默认阈值是 5）")
    for rule in matched_rules:
        print(f"    {format_rule(rule)}")


def main():
    keep_db = os.environ.get("PROBE_DB_PATH")
    if keep_db:
        db_path = Path(keep_db)
        db_path.unlink(missing_ok=True)
        store = EventStore(db_path)
        try:
            _run(store)
        finally:
            store.close()
        print(f"\n(数据库已保留: {db_path})")
        return
    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "probe.sqlite3"
        store = EventStore(db_path)
        try:
            _run(store)
        finally:
            store.close()


if __name__ == "__main__":
    main()
