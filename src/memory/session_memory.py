"""Deterministic incremental session memory and context budgeting."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable


_JSON_FENCE = re.compile(r"```json\s*(.*?)```", re.IGNORECASE | re.DOTALL)


@dataclass
class SessionMemory:
    version: int = 1
    summarized_turns: int = 0
    entries: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "summarized_turns": self.summarized_turns,
            "entries": self.entries,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any] | None) -> "SessionMemory":
        value = value or {}
        return cls(
            version=int(value.get("version") or 1),
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
    ):
        self.memory = memory or SessionMemory()
        self.budget_chars = max(2_000, int(budget_chars))
        self.recent_turns = max(2, int(recent_turns))
        self.on_update = on_update

    def build_context(self, transcript: list[dict[str, Any]]) -> str:
        cursor = min(self.memory.summarized_turns, len(transcript))
        keep_from = max(cursor, len(transcript) - self.recent_turns)
        raw_full = self._raw_text(transcript[cursor:])
        summary_text = self.render_summary()

        if len(summary_text) + len(raw_full) > self.budget_chars and keep_from > cursor:
            for index in range(cursor, keep_from):
                self.memory.entries.append(self._digest(index, transcript[index]))
            self.memory.summarized_turns = keep_from
            self._compact_entries()
            summary_text = self.render_summary()
            if self.on_update is not None:
                self.on_update(self.memory, summary_text)

        recent = self._raw_text(transcript[self.memory.summarized_turns:])
        context = self._join(summary_text, recent)
        if len(context) <= self.budget_chars:
            return context

        # Preserve the newest content and hard-cap pathological single turns.
        reserve = min(len(summary_text), self.budget_chars // 2)
        summary_tail = summary_text[-reserve:]
        recent_budget = self.budget_chars - len(summary_tail) - 32
        recent_tail = recent[-max(0, recent_budget):]
        return self._join(summary_tail, recent_tail)[-self.budget_chars:]

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
        if speaker == "VALIDATOR" or "验证未通过" in content:
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
            if entry.get("kind") in {"validator", "proposal"}
        ][-24:]
        recent = self.memory.entries[-24:]
        merged = {entry["turn"]: entry for entry in (*important, *recent)}
        self.memory.entries = [merged[key] for key in sorted(merged)]
