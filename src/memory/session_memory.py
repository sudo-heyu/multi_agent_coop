"""Deterministic incremental session memory and context budgeting."""

from __future__ import annotations

import re
import json
import threading
import hashlib
from dataclasses import dataclass, field
from typing import Any, Callable


_JSON_FENCE = re.compile(r"```json\s*(.*?)```", re.IGNORECASE | re.DOTALL)


@dataclass
class SessionMemory:
    version: int = 1
    revision: int = 0
    summarized_turns: int = 0
    entries: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "revision": self.revision,
            "summarized_turns": self.summarized_turns,
            "entries": self.entries,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any] | None) -> "SessionMemory":
        value = value or {}
        return cls(
            version=int(value.get("version") or 1),
            revision=int(value.get("revision") or 0),
            summarized_turns=int(value.get("summarized_turns") or 0),
            entries=list(value.get("entries") or []),
        )


class SessionMemoryManager:
    """Summarize old turns and build a bounded prompt context.

    This first implementation is intentionally deterministic: no background
    model and no hidden failure mode. A later iteration can replace digest()
    with a model summarizer while keeping the same persisted schema.
    """

    def __init__(
        self,
        memory: SessionMemory | None = None,
        *,
        budget_chars: int = 14_000,
        recent_turns: int = 6,
        on_update: Callable[[SessionMemory, str], None] | None = None,
        allow_llm: bool = False,
    ):
        self.memory = memory or SessionMemory()
        self.budget_chars = max(2_000, int(budget_chars))
        self.recent_turns = max(2, int(recent_turns))
        self.on_update = on_update
        self.allow_llm = bool(allow_llm)
        self._lock = threading.RLock()
        self._summary_inflight = False

    def build_context(self, transcript: list[dict[str, Any]]) -> str:
        with self._lock:
            cursor = min(self.memory.summarized_turns, len(transcript))
            keep_from = max(cursor, len(transcript) - self.recent_turns)
            raw_full = self._raw_text(transcript[cursor:])
            summary_text = self.render_summary()

            if len(summary_text) + len(raw_full) > self.budget_chars and keep_from > cursor:
                for index in range(cursor, keep_from):
                    self.memory.entries.append(self._digest(index, transcript[index]))
                self.memory.summarized_turns = keep_from
                self.memory.revision += 1
                self._compact_entries()
                summary_text = self.render_summary()
                if self.on_update is not None:
                    self.on_update(self.memory, summary_text)
                self._schedule_semantic_refinement()

            recent = self._raw_text(transcript[self.memory.summarized_turns:])
        context = self._join(summary_text, recent)
        if len(context) <= self.budget_chars:
            return context

        # Preserve the newest content and hard-cap pathological single turns.
        reserve = min(len(summary_text), self.budget_chars // 2)
        summary_tail = self._bounded_tail(summary_text, reserve)
        recent_budget = self.budget_chars - len(summary_tail) - 32
        recent_tail = self._bounded_tail(recent, max(0, recent_budget))
        return self._join(summary_tail, recent_tail)[-self.budget_chars:]

    @staticmethod
    def _bounded_tail(text: str, budget: int) -> str:
        """Trim at a line boundary so labels and JSON are not cut mid-token when possible."""
        if len(text) <= budget:
            return text
        tail = text[-budget:]
        newline = tail.find("\n")
        return tail[newline + 1:] if newline >= 0 else tail

    def render_summary(self) -> str:
        if not self.memory.entries:
            return ""
        lines = ["### 会话历史摘要（早期原文已压缩）"]
        for entry in self.memory.entries:
            lines.append(
                f"- turn={entry['turn']} speaker={entry['speaker']} "
                f"kind={entry['kind']}: {entry['digest']}"
            )
        return "\n".join(lines)

    @staticmethod
    def _join(summary: str, recent: str) -> str:
        parts = [part for part in (summary.strip(), recent.strip()) if part]
        return "\n\n".join(parts)

    @staticmethod
    def _raw_text(messages: list[dict[str, Any]]) -> str:
        return "\n\n".join(
            f"### {item.get('speaker', 'UNKNOWN')}\n{item.get('content', '')}"
            for item in messages
        )

    def _digest(self, index: int, item: dict[str, Any]) -> dict[str, Any]:
        speaker = str(item.get("speaker") or "UNKNOWN")
        content = " ".join(str(item.get("content") or "").split())
        lower = content.lower()
        explicit_kind = str(item.get("kind") or "").lower()
        if explicit_kind:
            kind = explicit_kind
        elif speaker == "VALIDATOR" or "验证未通过" in content:
            kind = "validator"
        elif '"agreed"' in lower or "同意" in content or "反对" in content:
            kind = "vote"
        elif _JSON_FENCE.search(str(item.get("content") or "")):
            kind = "proposal"
        elif "广播" in content:
            kind = "broadcast"
        else:
            kind = "message"
        match = _JSON_FENCE.search(str(item.get("content") or ""))
        if match and kind == "proposal":
            json_text = " ".join(match.group(1).split())
            digest = f"参数JSON={json_text[:500]}"
        else:
            digest = content[:500]
        return {"turn": index, "speaker": speaker, "kind": kind, "digest": digest}

    def _compact_entries(self) -> None:
        # Bound summary growth. Keep validator/proposal evidence preferentially.
        if len(self.memory.entries) <= 48:
            return
        important = [
            entry for entry in self.memory.entries
            if entry.get("kind") in {"validator", "proposal", "semantic"}
        ][-23:]
        recent = self.memory.entries[-23:]
        merged = {entry["turn"]: entry for entry in (*important, *recent)}
        dropped = [entry for entry in self.memory.entries if entry["turn"] not in merged]
        if dropped:
            digest = "；".join(
                f"{entry.get('speaker', 'UNKNOWN')}:{entry.get('digest', '')}"
                for entry in dropped
            )[:1200]
            first = min(int(entry["turn"]) for entry in dropped)
            merged[first] = {
                "turn": first,
                "turn_end": max(int(entry["turn"]) for entry in dropped),
                "speaker": "MEMORY",
                "kind": "semantic",
                "digest": digest,
            }
        self.memory.entries = [merged[key] for key in sorted(merged)]
        self.memory.entries = self.memory.entries[-48:]

    def _schedule_semantic_refinement(self) -> None:
        """批量、异步精炼旧摘要；daemon worker 不延长协商或进程退出时间。"""
        from .llm_backend import session_enabled
        with self._lock:
            candidates = [e for e in self.memory.entries if e.get("kind") != "semantic"]
            if (not self.allow_llm or not session_enabled()
                    or self._summary_inflight or len(candidates) < 12):
                return
            # Never send private overlays to an external summarizer.
            batch = [e for e in candidates[:-6] if e.get("speaker") != "PRIVATE_MEMORY"]
            if len(batch) < 6:
                return
            self._summary_inflight = True
            expected_revision = self.memory.revision
            source_hash = hashlib.sha256(
                json.dumps(batch, ensure_ascii=False, sort_keys=True).encode()
            ).hexdigest()
        threading.Thread(
            target=self._refine_batch,
            args=(batch, expected_revision, source_hash), daemon=True,
            name="session-memory-refiner",
        ).start()

    def _refine_batch(
        self, batch: list[dict[str, Any]], expected_revision: int, source_hash: str
    ) -> None:
        from .llm_backend import summarize
        turns = {int(e["turn"]) for e in batch}
        try:
            prompt = (
                "把以下早期协商记录压缩成不超过300字的持久会话摘要。保留：各方约束、"
                "提案参数、反对理由、Validator失败项、已达成决定和未解决问题；不得编造。\n"
                + json.dumps(batch, ensure_ascii=False)[:14000]
            )
            digest = summarize(prompt)
            semantic = {"turn": min(turns), "turn_end": max(turns),
                        "speaker": "MEMORY", "kind": "semantic", "digest": digest[:1200],
                        "source_hash": source_hash, "source_revision": expected_revision,
                        "model": __import__("src.memory.llm_backend", fromlist=["model_name"]).model_name(),
                        "prompt_version": 1}
            with self._lock:
                if self.memory.revision != expected_revision:
                    return
                self.memory.entries = [
                    e for e in self.memory.entries if int(e.get("turn", -1)) not in turns
                ] + [semantic]
                self.memory.entries.sort(key=lambda e: int(e.get("turn", -1)))
                self.memory.revision += 1
                summary_text = self.render_summary()
            if self.on_update is not None:
                self.on_update(self.memory, summary_text)
        except Exception:
            pass  # 确定性摘要仍完整可用；后台失败不得影响会话。
        finally:
            with self._lock:
                self._summary_inflight = False
