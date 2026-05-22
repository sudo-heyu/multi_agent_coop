import json
import requests
from pathlib import Path
from typing import Callable, Iterator

OLLAMA_URL = "http://localhost:11434/api/chat"
DEFAULT_MODEL = "qwen3:14b"
MAX_TOOL_ROUNDS = 8  # 防止工具调用死循环

_HTTP = requests.Session()
_HTTP.trust_env = False


class APAgent:
    def __init__(self, agent_id: str, agents_dir: Path, model: str = DEFAULT_MODEL):
        self.agent_id = agent_id
        self.name = agent_id.upper()
        self.model = model
        self.system_prompt = self._build_system_prompt(agents_dir / agent_id)

    def _build_system_prompt(self, agent_path: Path) -> str:
        parts = []
        for fname in ["IDENTITY.md", "SOUL.md", "AGENTS.md", "TOOLS.md"]:
            fpath = agent_path / fname
            if fpath.exists():
                parts.append(fpath.read_text(encoding="utf-8").strip())
        prompt = "\n\n---\n\n".join(parts)
        prompt += (
            "\n\n---\n\n"
            "输出格式要求：用纯文本段落输出，不使用任何 Markdown 格式符号"
            "（不使用 **加粗**、*斜体*、# 标题、- 列表、`代码` 等标记）。"
            "列表内容改写为连贯的自然语言句子，直接分段即可。"
        )
        return prompt

    def _build_messages(self, conversation_log: list[dict], instruction: str) -> list[dict]:
        """构造发送给 Ollama 的 messages。"""
        if conversation_log:
            transcript = "\n\n".join(
                f"### {msg['speaker']}\n{msg['content']}"
                for msg in conversation_log
            )
            user_content = (
                f"当前对话记录：\n\n{transcript}\n\n"
                f"{'─' * 40}\n\n"
                f"{instruction}"
            )
        else:
            user_content = instruction

        return [
            {"role": "system", "content": self.system_prompt},
            {"role": "user",   "content": user_content},
        ]

    def _request_chat(self, messages: list[dict], tools: list[dict] | None = None) -> dict:
        """执行一次非流式 chat 请求，用于工具调用轮。"""
        payload: dict = {
            "model":  self.model,
            "think":  False,
            "stream": False,
            "messages": messages,
        }
        if tools:
            payload["tools"] = tools

        resp = _HTTP.post(OLLAMA_URL, json=payload, timeout=180)
        resp.raise_for_status()
        return resp.json()["message"]

    def _stream_chat(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
    ) -> Iterator[str]:
        """执行一次流式 chat 请求，逐块产出最终自然语言内容。"""
        payload: dict = {
            "model":  self.model,
            "think":  False,
            "stream": True,
            "messages": messages,
        }
        if tools:
            payload["tools"] = tools

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
        instruction: str,
        tools: list[dict] | None = None,
        tool_executor: Callable | None = None,
        tool_log: list | None = None,
        tool_callback: Callable[[str, dict, dict, float], None] | None = None,
    ) -> Iterator[str]:
        """
        生成本轮发言，支持多轮工具调用；最终自然语言回复流式产出。

        Args:
            conversation_log: 共享对话历史
            instruction:      本阶段协调者指令
            tools:            工具 schema 列表（None 表示不启用工具）
            tool_executor:    executor(tool_name, args) -> (result_dict, duration_ms)
            tool_log:         若提供，每次工具调用追加记录至此列表
            tool_callback:    工具执行完成后的控制台输出回调

        Yields:
            agent 最终文本回复的流式 content 片段。
        """
        messages = self._build_messages(conversation_log, instruction)

        if not tools:
            yield from self._stream_chat(messages)
            return

        # 全程流式：每轮同时收集 content（实时 yield）和 tool_calls（流结束后处理）。
        # 工具调用轮 content 通常为空，不会产生多余输出；
        # 最终文本轮无 tool_calls，content 已实时流出，直接 return。
        for _ in range(MAX_TOOL_ROUNDS):
            payload: dict = {
                "model":    self.model,
                "think":    False,
                "stream":   True,
                "messages": messages,
                "tools":    tools,
            }

            round_content: list[str] = []
            round_tool_calls: list[dict] = []

            with _HTTP.post(OLLAMA_URL, json=payload, timeout=180, stream=True) as resp:
                resp.raise_for_status()
                for line in resp.iter_lines(decode_unicode=True):
                    if not line:
                        continue
                    chunk = json.loads(line)
                    msg = chunk.get("message") or {}

                    piece = msg.get("content") or ""
                    if piece:
                        round_content.append(piece)
                        yield piece  # 实时流出

                    if msg.get("tool_calls"):
                        round_tool_calls.extend(msg["tool_calls"])

            if not round_tool_calls:
                # 没有工具调用：文本已全部实时产出，本轮结束
                return

            # 将 assistant 的工具调用追加进对话
            messages.append({
                "role":       "assistant",
                "content":    "".join(round_content),
                "tool_calls": round_tool_calls,
            })

            # 逐个执行工具，将结果追加进对话
            for tc in round_tool_calls:
                fn        = tc.get("function", {})
                tool_name = fn.get("name", "")
                raw_args  = fn.get("arguments", {})

                if isinstance(raw_args, str):
                    try:
                        raw_args = json.loads(raw_args)
                    except json.JSONDecodeError:
                        raw_args = {}

                if tool_executor:
                    result_dict, dur_ms = tool_executor(tool_name, raw_args)
                else:
                    result_dict, dur_ms = {"error": "no executor configured"}, 0.0

                if tool_callback:
                    tool_callback(tool_name, raw_args, result_dict, dur_ms)

                tool_message = {
                    "role":    "tool",
                    "content": json.dumps(result_dict, ensure_ascii=False),
                    "name":    tool_name,
                }
                if tc.get("id"):
                    tool_message["tool_call_id"] = tc["id"]
                messages.append(tool_message)

                if tool_log is not None:
                    tool_log.append({
                        "tool":        tool_name,
                        "input":       raw_args,
                        "output":      result_dict,
                        "duration_ms": dur_ms,
                    })

        # 超出最大轮数：强制要求输出文本
        messages.append({
            "role":    "user",
            "content": "请根据以上工具结果直接给出最终回复，不要再调用工具。",
        })
        yield from self._stream_chat(messages)

    def speak(
        self,
        conversation_log: list[dict],
        instruction: str,
        tools: list[dict] | None = None,
        tool_executor: Callable | None = None,
        tool_log: list | None = None,
    ) -> str:
        """生成本轮完整发言，保留给非流式调用路径使用。"""
        return "".join(
            self.speak_stream(
                conversation_log,
                instruction,
                tools=tools,
                tool_executor=tool_executor,
                tool_log=tool_log,
            )
        ).strip()
