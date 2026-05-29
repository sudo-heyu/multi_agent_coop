import json
import os
import requests
from pathlib import Path
from typing import Callable, Iterator

# 尝试加载 .env（文件不存在或未安装 dotenv 时静默跳过）
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent.parent / ".env")
except ImportError:
    pass

OLLAMA_URL = "http://localhost:11434/api/chat"
DEFAULT_MODEL = "qwen3:14b"
MAX_TOOL_ROUNDS = 8  # 防止工具调用死循环

# PPIO 派欧云
PPIO_ALIAS = "qwen:80b"
PPIO_MODEL = "qwen/qwen3-next-80b-a3b-instruct"
PPIO_URL = "https://api.ppio.com/openai/v1/chat/completions"

_HTTP = requests.Session()
_HTTP.trust_env = False


class APAgent:
    def __init__(self, agent_id: str, agents_dir: Path, model: str = DEFAULT_MODEL):
        self.agent_id = agent_id
        self.name = agent_id.upper()
        self.model = model
        self.system_prompt = self._build_system_prompt(agents_dir / agent_id)

        if model == PPIO_ALIAS:
            api_key = os.environ.get("PPIO_API_KEY", "")
            if not api_key:
                raise RuntimeError(
                    "使用 qwen:80b 需要设置 PPIO_API_KEY，请在 .env 文件中填写。"
                )
            self._ppio_headers = {
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            }
        else:
            self._ppio_headers = None

    @property
    def _use_ppio(self) -> bool:
        return self.model == PPIO_ALIAS

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
        """构造发送给模型的 messages。"""
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
        """执行一次非流式 chat 请求，返回标准化 message dict。"""
        if self._use_ppio:
            return self._request_chat_ppio(messages, tools)

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

    def _request_chat_ppio(self, messages: list[dict], tools: list[dict] | None = None) -> dict:
        """PPIO OpenAI-compatible 非流式请求，返回与 Ollama 相同结构的 message dict。"""
        payload: dict = {
            "model":  PPIO_MODEL,
            "stream": False,
            "messages": messages,
        }
        if tools:
            payload["tools"] = tools

        payload_bytes = len(json.dumps(payload, ensure_ascii=False).encode())
        print(f"  [PPIO] 请求大小: {payload_bytes} bytes / {payload_bytes//4} 估算tokens  消息数: {len(messages)}", flush=True)
        resp = _HTTP.post(PPIO_URL, headers=self._ppio_headers, json=payload, timeout=180)
        if not resp.ok:
            try:
                err_body = resp.json()
            except Exception:
                err_body = resp.text
            raise RuntimeError(
                f"PPIO {resp.status_code}: {err_body}"
            )
        msg = resp.json()["choices"][0]["message"]
        # 统一格式：确保 tool_calls 字段存在
        return {
            "role":       msg.get("role", "assistant"),
            "content":    msg.get("content") or "",
            "tool_calls": msg.get("tool_calls") or [],
        }

    def _stream_chat(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
    ) -> Iterator[str]:
        """执行一次流式 chat 请求，逐块产出最终自然语言内容。"""
        if self._use_ppio:
            yield from self._stream_chat_ppio(messages, tools)
            return

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
            for raw in resp.iter_lines():
                if not raw:
                    continue
                chunk = json.loads(raw.decode("utf-8"))
                content = (chunk.get("message") or {}).get("content") or ""
                if content:
                    yield content

    def _stream_chat_ppio(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
    ) -> Iterator[str]:
        """PPIO OpenAI SSE 流式请求，逐块产出内容。"""
        payload: dict = {
            "model":  PPIO_MODEL,
            "stream": True,
            "messages": messages,
        }
        if tools:
            payload["tools"] = tools

        with _HTTP.post(PPIO_URL, headers=self._ppio_headers,
                        json=payload, timeout=180, stream=True) as resp:
            resp.raise_for_status()
            for raw in resp.iter_lines():
                if not raw:
                    continue
                line = raw.decode("utf-8")
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    chunk = json.loads(data)
                except json.JSONDecodeError:
                    continue
                delta = (chunk.get("choices") or [{}])[0].get("delta") or {}
                content = delta.get("content") or ""
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

        if self._use_ppio:
            yield from self._speak_stream_ppio(messages, tools, tool_executor, tool_log, tool_callback)
            return

        # Ollama 全程流式：每轮同时收集 content（实时 yield）和 tool_calls（流结束后处理）。
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
                for raw in resp.iter_lines():
                    if not raw:
                        continue
                    chunk = json.loads(raw.decode("utf-8"))
                    msg = chunk.get("message") or {}

                    piece = msg.get("content") or ""
                    if piece:
                        round_content.append(piece)
                        yield piece  # 实时流出

                    if msg.get("tool_calls"):
                        round_tool_calls.extend(msg["tool_calls"])

            if not round_tool_calls:
                return

            messages.append({
                "role":       "assistant",
                "content":    "".join(round_content),
                "tool_calls": round_tool_calls,
            })

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

    def _speak_stream_ppio(
        self,
        messages: list[dict],
        tools: list[dict],
        tool_executor: Callable | None,
        tool_log: list | None,
        tool_callback: Callable[[str, dict, dict, float], None] | None,
    ) -> Iterator[str]:
        """PPIO 后端的工具调用循环：工具轮用非流式，最终回复用流式。"""
        for _ in range(MAX_TOOL_ROUNDS):
            msg = self._request_chat_ppio(messages, tools)
            tool_calls = msg.get("tool_calls") or []

            if not tool_calls:
                # 无工具调用：直接 yield 已取得的内容，不再重发请求
                content = msg.get("content") or ""
                if content:
                    yield content
                return

            messages.append({
                "role":       "assistant",
                "content":    msg.get("content") or "",
                "tool_calls": tool_calls,
            })

            for tc in tool_calls:
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

        # 超出最大轮数
        messages.append({
            "role":    "user",
            "content": "请根据以上工具结果直接给出最终回复，不要再调用工具。",
        })
        yield from self._stream_chat_ppio(messages)

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
