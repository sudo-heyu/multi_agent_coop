import json
import re
import requests
from pathlib import Path
from typing import Iterator

OLLAMA_URL = "http://localhost:11434/api/chat"
DEFAULT_MODEL = "qwen3:14b"

_HTTP = requests.Session()
_HTTP.trust_env = False

_VALID_PROPOSERS = {"ap1", "ap2", "ap3", "none"}
_VALID_STRATEGIES = {"co_sr", "co_edca", "joint", "noop"}


class CoordinatorAgent:
    def __init__(self, agents_dir: Path, model: str = DEFAULT_MODEL):
        self.model = model
        self.system_prompt = self._build_system_prompt(agents_dir / "coordinator")

    def _build_system_prompt(self, agent_path: Path) -> str:
        parts = []
        for fname in ["IDENTITY.md", "SOUL.md", "AGENTS.md"]:
            fpath = agent_path / fname
            if fpath.exists():
                parts.append(fpath.read_text(encoding="utf-8").strip())
        prompt = "\n\n---\n\n".join(parts)
        prompt += (
            "\n\n---\n\n"
            "输出格式要求：用纯文本段落输出，不使用任何 Markdown 格式符号"
            "（不使用 **加粗**、*斜体*、# 标题、- 列表符号等）。"
            "JSON 代码块仍需正常输出。"
        )
        return prompt

    def _stream_chat(self, messages: list[dict]) -> Iterator[str]:
        payload = {
            "model": self.model,
            "think": False,
            "stream": True,
            "messages": messages,
        }
        with _HTTP.post(OLLAMA_URL, json=payload, timeout=180, stream=True) as resp:
            resp.raise_for_status()
            for line in resp.iter_lines(decode_unicode=True):
                if not line:
                    continue
                chunk = json.loads(line)
                content = (chunk.get("message") or {}).get("content") or ""
                if content:
                    yield content

    def speak_stream(
        self,
        conversation_log: list[dict],
        ap_state: dict,
    ) -> Iterator[str]:
        """
        读取广播记录，流式产出协调决策文本。
        调用方收集所有 chunk 后调用 parse_decision() 提取结构化结果。
        """
        transcript = "\n\n".join(
            f"### {msg['speaker']}\n{msg['content']}"
            for msg in conversation_log
        )
        state_json = json.dumps(ap_state, ensure_ascii=False, indent=2)
        user_content = (
            f"以下是三台AP的广播发言：\n\n{transcript}\n\n"
            f"{'─' * 40}\n\n"
            f"原始状态数据（供参考）：\n{state_json}\n\n"
            "请基于以上信息，决定提案方和协商路径。"
        )
        messages = [
            {"role": "system", "content": self.system_prompt},
            {"role": "user", "content": user_content},
        ]
        yield from self._stream_chat(messages)

    def parse_decision(self, text: str) -> tuple[str, str]:
        """
        从协调者回复中提取 (proposer_id, strategy)。
        解析失败时返回 ("ap1", "noop") 作为保底。
        """
        data = None

        for m in re.finditer(r"```(?:json)?\s*(.*?)\s*```", text, re.DOTALL):
            try:
                parsed = json.loads(m.group(1).strip())
                if isinstance(parsed, dict):
                    data = parsed
                    break
            except json.JSONDecodeError:
                pass

        if data is None:
            decoder = json.JSONDecoder()
            for m in re.finditer(r"\{", text):
                try:
                    parsed, _ = decoder.raw_decode(text[m.start():])
                    if isinstance(parsed, dict):
                        data = parsed
                        break
                except json.JSONDecodeError:
                    pass

        if data is None:
            return "ap1", "noop"

        proposer = str(data.get("proposer", "none")).lower().strip()
        strategy = str(data.get("strategy", "noop")).lower().strip()

        if proposer not in _VALID_PROPOSERS:
            proposer = "none"
        if strategy not in _VALID_STRATEGIES:
            strategy = "noop"

        return proposer, strategy
