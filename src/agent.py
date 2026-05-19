import requests
from pathlib import Path

OLLAMA_URL = "http://localhost:11434/api/chat"
DEFAULT_MODEL = "qwen3:14b"


class APAgent:
    def __init__(self, agent_id: str, agents_dir: Path, model: str = DEFAULT_MODEL):
        self.agent_id = agent_id
        self.name = agent_id.upper()          # "ap1" -> "AP1"
        self.model = model
        self.system_prompt = self._build_system_prompt(agents_dir / agent_id)

    def _build_system_prompt(self, agent_path: Path) -> str:
        parts = []
        for fname in ["IDENTITY.md", "SOUL.md", "AGENTS.md"]:
            fpath = agent_path / fname
            if fpath.exists():
                parts.append(fpath.read_text(encoding="utf-8").strip())
        return "\n\n---\n\n".join(parts)

    def speak(self, conversation_log: list[dict], instruction: str) -> str:
        """
        Generate a response for this agent's current turn.

        Args:
            conversation_log: shared history, list of {"speaker": str, "content": str}
            instruction:       phase-specific directive from the orchestrator

        Returns:
            The agent's response as a plain string.
        """
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

        payload = {
            "model": self.model,
            "think": False,
            "stream": False,
            "messages": [
                {"role": "system", "content": self.system_prompt},
                {"role": "user",   "content": user_content},
            ],
        }

        resp = requests.post(OLLAMA_URL, json=payload, timeout=180)
        resp.raise_for_status()
        return resp.json()["message"]["content"].strip()
