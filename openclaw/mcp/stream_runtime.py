"""PPIO streaming AP runtime.

This runtime is used by run.py.  It keeps the same relay, memory, workspace,
validator, executor and outcome pipeline as the OpenClaw path, but replaces the
per-turn `openclaw agent` subprocess with a direct OpenAI-compatible streaming
chat/completions call.
"""
from __future__ import annotations

import json
import os
import time
import urllib.parse
from pathlib import Path
from typing import Any

import requests

from src.memory.workspace import workspace

try:
    import direct_tools
    import tool_policy
except ImportError:  # pragma: no cover - package import fallback
    from . import direct_tools  # type: ignore
    from . import tool_policy  # type: ignore


def _orchestration_module():
    try:
        import orchestration as orch
    except ImportError:  # pragma: no cover - package import fallback
        from openclaw.mcp import orchestration as orch  # type: ignore
    return orch


DEFAULT_MODEL = "qwen/qwen3.6-35b-a3b"
DEFAULT_BASE_URL = "https://api.ppio.com/openai/v1"
LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}


def _truthy(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _memory_enabled() -> bool:
    value = os.environ.get("MULTIAP_MEMORY_MODE", "on").strip().lower()
    return value not in {"0", "false", "no", "off", "none", "disabled"}


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, str(default)))
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except ValueError:
        return default


def _load_ppio_key() -> str:
    key = os.environ.get("PPIO_API_KEY", "").strip()
    if key:
        return key
    env_file = Path(__file__).resolve().parents[2] / ".env"
    try:
        for line in env_file.read_text(encoding="utf-8").splitlines():
            if line.startswith("PPIO_API_KEY="):
                return line.split("=", 1)[1].strip().strip("'\"")
    except OSError:
        pass
    raise RuntimeError("PPIO_API_KEY is required for run.py stream runtime")


def _truncate_tool_result(value: Any, limit: int = 12_000) -> str:
    text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    if len(text) <= limit:
        return text
    return text[:limit] + "...[truncated]"


def _read_workspace_system(agent_id: str) -> str:
    root = workspace(agent_id)
    parts: list[str] = []
    for name in ("IDENTITY.md", "SOUL.md", "AGENTS.md", "TOOLS.md"):
        path = root / name
        try:
            text = path.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if text:
            parts.append(f"【{name}】\n{text}")
    return "\n\n".join(parts)


def _merge_tool_call_delta(target: dict[str, Any], delta: dict[str, Any]) -> None:
    if delta.get("id"):
        target["id"] = delta["id"]
    if delta.get("type"):
        target["type"] = delta["type"]
    fn_delta = delta.get("function") or {}
    fn = target.setdefault("function", {"name": "", "arguments": ""})
    if fn_delta.get("name"):
        fn["name"] = fn_delta["name"]
    if fn_delta.get("arguments"):
        fn["arguments"] = fn.get("arguments", "") + fn_delta["arguments"]


class PPIOStreamRuntime:
    def __init__(
        self,
        *,
        model: str | None = None,
        base_url: str | None = None,
        api_key: str | None = None,
        timeout: float | None = None,
        max_tool_rounds: int | None = None,
    ) -> None:
        self.model = (
            model
            or os.environ.get("MULTIAP_STREAM_MODEL")
            or os.environ.get("MULTIAP_PPIO_MODEL_ID")
            or DEFAULT_MODEL
        )
        self.base_url = (
            base_url
            or os.environ.get("MULTIAP_PPIO_BASE_URL")
            or os.environ.get("MULTIAP_MEMORY_PPIO_BASE_URL")
            or DEFAULT_BASE_URL
        ).rstrip("/")
        self.api_key = api_key or _load_ppio_key()
        self.timeout = timeout if timeout is not None else _env_float("MULTIAP_STREAM_TIMEOUT", 180.0)
        self.max_tool_rounds = (
            max_tool_rounds
            if max_tool_rounds is not None else
            _env_int("MULTIAP_STREAM_MAX_TOOL_ROUNDS", 6)
        )
        self.temperature = _env_float("MULTIAP_STREAM_TEMPERATURE", 0.1)
        self.max_tokens = _env_int("MULTIAP_STREAM_MAX_TOKENS", 1600)
        self.request_retries = max(1, _env_int("MULTIAP_STREAM_REQUEST_RETRIES", 4))
        self.retry_delay = max(0.0, _env_float("MULTIAP_STREAM_RETRY_DELAY", 3.0))

    @property
    def model_label(self) -> str:
        return f"ppio-stream/{self.model}"

    def speak(
        self,
        ap_id: str,
        instruction: str,
        thinking: str = "off",
        extra_env: dict[str, str] | None = None,
        on_text_delta=None,
    ) -> str:
        """Run one AP turn and return the final visible assistant text."""
        orch = _orchestration_module()

        ap = ap_id.lower()
        orch._SESSION.refresh_agent_workspace(ap)
        transcript = orch._SESSION.transcript_text(ap)
        orch._SESSION.sync_agent_workspace(ap)
        user_message = orch._build_agent_message(
            ap,
            transcript,
            instruction,
            shared_warnings=orch._SESSION.recalled_warnings,
            shared_positive=orch._SESSION.recalled_episodes,
            shared_rules=orch._SESSION.recalled_rules,
        )
        messages = [
            {"role": "system", "content": self._system_prompt(ap)},
            {"role": "user", "content": user_message},
        ]
        return self._chat_loop(ap, messages, on_text_delta)

    def _system_prompt(self, ap_id: str) -> str:
        workspace_text = _read_workspace_system(ap_id)
        tool_profile = os.environ.get("MULTIAP_TOOL_PROFILE", "full").strip().lower()
        visible_tool_profile = tool_policy.agent_visible_profile(tool_profile)
        evidence_basis = (
            "已给状态、对话记录、历史记忆和自身推理"
            if _memory_enabled()
            else "已给状态、当前对话记录和自身推理"
        )
        fallback_basis = (
            "最新状态、历史记忆和可用验算工具"
            if _memory_enabled()
            else "最新状态、当前对话记录和可用验算工具"
        )
        if not direct_tools.openai_tools():
            tool_protocol = (
                "你运行在 Multi-AP stream runtime 中。当前工具能力档位为 "
                f"{visible_tool_profile}，本回合没有任何可调用工具。"
                f"不要声称调用过工具或引用工具返回；只能基于{evidence_basis}"
                "完成当前阶段。只完成当前用户指令指定的阶段，不替其他 AP 发言。"
                "最终面向用户的回复使用中文自然语言；需要 JSON 时必须输出可解析 JSON 代码块。"
            )
        else:
            tool_protocol = (
                "你运行在 Multi-AP stream runtime 中。你拥有与 OpenClaw MCP 路径同名的工具；"
                "需要实时状态、STA 反馈或参数验算时，请使用原生 tool_calls 调用工具，"
                "不要伪造工具返回。收到工具结果后再继续回答。"
                f"当前工具能力档位为 {visible_tool_profile}；若某工具 schema 不存在或返回不可用，"
                f"请改用{fallback_basis}完成当前阶段。"
                "只完成当前用户指令指定的阶段，不替其他 AP 发言。"
                "最终面向用户的回复使用中文自然语言；需要 JSON 时必须输出可解析 JSON 代码块。"
            )
        return f"{tool_protocol}\n\n{workspace_text}".strip()

    def _request_payload(self, messages: list[dict[str, Any]]) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.model,
            "stream": True,
            "messages": messages,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
        }
        tools = direct_tools.openai_tools()
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        extra_raw = os.environ.get("MULTIAP_STREAM_EXTRA_BODY", "").strip()
        if extra_raw:
            try:
                extra = json.loads(extra_raw)
                if isinstance(extra, dict):
                    payload.update(extra)
            except json.JSONDecodeError:
                pass
        if _truthy(os.environ.get("MULTIAP_STREAM_DISABLE_THINKING")):
            # Some Qwen-compatible deployments accept this extension.  It is
            # opt-in because strict OpenAI-compatible servers may reject unknown
            # request fields.
            payload.setdefault("enable_thinking", False)
        return payload

    def _session(self) -> requests.Session:
        sess = requests.Session()
        host = (urllib.parse.urlparse(self.base_url).hostname or "").lower()
        if host in LOCAL_HOSTS:
            sess.trust_env = False
        return sess

    def _chat_loop(self, ap_id: str, messages: list[dict[str, Any]], on_text_delta) -> str:
        visible_parts: list[str] = []
        empty_after_tool_retries = 0
        for _ in range(max(1, self.max_tool_rounds + 3)):
            text, tool_calls = self._stream_once(messages, on_text_delta)
            if text:
                visible_parts.append(text)
            if not tool_calls:
                reply = "".join(visible_parts).strip()
                if not reply:
                    if (
                        messages
                        and messages[-1].get("role") == "tool"
                        and empty_after_tool_retries < 2
                    ):
                        empty_after_tool_retries += 1
                        messages.append({
                            "role": "user",
                            "content": (
                                "工具结果已返回。请基于这些工具结果继续完成当前阶段发言；"
                                "不要再次输出空内容。"
                            ),
                        })
                        continue
                    raise RuntimeError("stream runtime returned empty assistant content")
                return reply
            for index, call in enumerate(tool_calls):
                call.setdefault("id", f"call_{index}")
            assistant_msg = {"role": "assistant", "content": text or "", "tool_calls": tool_calls}
            messages.append(assistant_msg)
            for call in tool_calls:
                result = self._execute_tool(ap_id, call)
                messages.append({
                    "role": "tool",
                    "tool_call_id": call.get("id") or call.get("function", {}).get("name", "tool"),
                    "name": call.get("function", {}).get("name", ""),
                    "content": _truncate_tool_result(result),
                })
        raise RuntimeError(f"stream runtime exceeded max tool rounds ({self.max_tool_rounds})")

    def _stream_once(self, messages: list[dict[str, Any]], on_text_delta) -> tuple[str, list[dict[str, Any]]]:
        last_error: Exception | None = None
        for attempt in range(1, self.request_retries + 1):
            try:
                return self._stream_once_attempt(messages, on_text_delta)
            except requests.RequestException as exc:
                last_error = exc
            except RuntimeError as exc:
                last_error = exc
                if "PPIO stream API HTTP 5" not in str(exc) and "HTTP 429" not in str(exc):
                    raise
            if attempt < self.request_retries:
                time.sleep(self.retry_delay * attempt)
        assert last_error is not None
        raise last_error

    def _stream_once_attempt(
        self,
        messages: list[dict[str, Any]],
        on_text_delta,
    ) -> tuple[str, list[dict[str, Any]]]:
        payload = self._request_payload(messages)
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        }
        url = f"{self.base_url}/chat/completions"
        sess = self._session()
        try:
            resp = sess.post(url, json=payload, headers=headers, stream=True, timeout=self.timeout)
            if resp.status_code >= 400:
                body = resp.text[:1000]
                raise RuntimeError(f"PPIO stream API HTTP {resp.status_code}: {body}")
            text_parts: list[str] = []
            tool_calls_by_index: dict[int, dict[str, Any]] = {}
            for raw_line in resp.iter_lines(decode_unicode=False):
                if not raw_line:
                    continue
                if isinstance(raw_line, bytes):
                    line = raw_line.decode("utf-8", errors="replace").strip()
                else:
                    line = str(raw_line).strip()
                if line.startswith("data:"):
                    line = line[5:].strip()
                if not line or line == "[DONE]":
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                choices = event.get("choices") or []
                if not choices:
                    continue
                delta = choices[0].get("delta") or {}
                content = delta.get("content")
                if isinstance(content, str) and content:
                    text_parts.append(content)
                    if on_text_delta is not None:
                        on_text_delta(content)
                for call_delta in delta.get("tool_calls") or []:
                    index = int(call_delta.get("index", 0))
                    target = tool_calls_by_index.setdefault(index, {
                        "id": "",
                        "type": "function",
                        "function": {"name": "", "arguments": ""},
                    })
                    _merge_tool_call_delta(target, call_delta)
            tool_calls = [
                call for _, call in sorted(tool_calls_by_index.items())
                if (call.get("function") or {}).get("name")
            ]
            return "".join(text_parts), tool_calls
        finally:
            sess.close()

    def _execute_tool(self, ap_id: str, call: dict[str, Any]) -> Any:
        orch = _orchestration_module()

        function = call.get("function") or {}
        name = str(function.get("name") or "")
        raw_args = function.get("arguments") or "{}"
        try:
            args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
            if not isinstance(args, dict):
                args = {}
        except json.JSONDecodeError:
            args = {}
        try:
            result, dur_ms = direct_tools.call_tool(name, args)
        except Exception as exc:  # noqa: BLE001
            result = {"error": f"{type(exc).__name__}: {exc}"}
            dur_ms = None
        try:
            orch._log_mcp_tool(ap_id, name, args, result, dur_ms)
            if orch._tool_callback is not None:
                orch._tool_callback(name, args, result, dur_ms)
        except Exception:
            pass
        return result
