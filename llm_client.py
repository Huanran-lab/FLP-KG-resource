"""Shared DeepSeek OpenAI-compatible client (raw httpx; avoids jiter/App Control)."""
from __future__ import annotations

from typing import Any

import httpx

from .config_utils import load_config, read_api_key


class HttpLLM:
    """Minimal chat-completions client over httpx."""

    def __init__(self, cfg: dict[str, Any] | None = None) -> None:
        self.cfg = cfg or load_config()
        timeout = float(self.cfg["llm"].get("timeout_s", 120))
        self._http = httpx.Client(trust_env=False, timeout=timeout)
        self.api_key = read_api_key(self.cfg)
        self.base_url = self.cfg["llm"]["base_url"].rstrip("/")
        self.model = self.cfg["llm"]["model"]
        self.default_temperature = float(self.cfg["llm"].get("temperature", 0.1))
        self.max_tokens = int(self.cfg["llm"].get("max_tokens", 4096))

    def close(self) -> None:
        self._http.close()

    def chat(
        self,
        system: str,
        user: str,
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
        json_mode: bool = False,
    ) -> str:
        payload: dict[str, Any] = {
            "model": self.model,
            "temperature": self.default_temperature if temperature is None else temperature,
            "max_tokens": self.max_tokens if max_tokens is None else max_tokens,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "thinking": {"type": "disabled"},
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        resp = self._http.post(
            f"{self.base_url}/chat/completions",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
        )
        resp.raise_for_status()
        data = resp.json()
        return (data["choices"][0]["message"].get("content") or "").strip()


def make_llm_client(cfg: dict[str, Any] | None = None) -> HttpLLM:
    """Backward-compatible factory; returns HttpLLM (not OpenAI SDK)."""
    return HttpLLM(cfg)
