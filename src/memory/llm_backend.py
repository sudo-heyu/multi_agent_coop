"""PPIO-backed LLM client for semantic and session-memory synthesis."""

from __future__ import annotations

import json
import os
import urllib.request
import urllib.parse
from typing import Any
from pathlib import Path


DEFAULT_PPIO_MODEL = "qwen/qwen3.6-35b-a3b"


def enabled() -> bool:
    return os.environ.get("MULTIAP_MEMORY_LLM", "1").lower() in {"1", "true", "yes", "on"}


def session_enabled() -> bool:
    """Session transcripts may contain private SLA; external synthesis is opt-in."""
    return os.environ.get("MULTIAP_SESSION_MEMORY_LLM", "0").lower() in {
        "1", "true", "yes", "on"
    }


def model_name() -> str:
    return os.environ.get(
        "MULTIAP_MEMORY_PPIO_MODEL",
        os.environ.get("MULTIAP_PPIO_MODEL_ID", DEFAULT_PPIO_MODEL),
    )


def _api_key() -> str:
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
    raise RuntimeError("PPIO_API_KEY is required for memory LLM synthesis")


def summarize(prompt: str, *, timeout: float = 90.0) -> str:
    """Call PPIO's OpenAI-compatible API. Raises on failure for safe degradation."""
    host = os.environ.get("MULTIAP_MEMORY_PPIO_BASE_URL", "https://api.ppio.com/openai/v1").rstrip("/")
    payload = json.dumps({
        "model": model_name(), "stream": False,
        "messages": [
            {"role": "system", "content": (
                "你是网络协商记忆整理器。只能根据提供的证据总结，不得补充事实。"
                "输出简洁中文纯文本，不输出思考过程。"
            )},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.1,
    }, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        host + "/chat/completions", data=payload,
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {_api_key()}"}, method="POST",
    )
    hostname = (urllib.parse.urlparse(host).hostname or "").lower()
    if hostname in {"127.0.0.1", "localhost", "::1"}:
        # Development PPIO-compatible loopback endpoints must never traverse proxies.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        response_ctx = opener.open(request, timeout=timeout)
    else:
        response_ctx = urllib.request.urlopen(request, timeout=timeout)
    with response_ctx as response:
        result: dict[str, Any] = json.loads(response.read().decode("utf-8"))
    choices = result.get("choices") or []
    text = str(((choices[0] if choices else {}).get("message") or {}).get("content") or "").strip()
    if not text:
        raise ValueError("memory LLM returned empty content")
    return text
