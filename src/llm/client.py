"""OpenAI 兼容 LLM 客户端封装。

默认接 NVIDIA API Catalog（GLM / DeepSeek），换任何 OpenAI 兼容供应商
只需改配置里的 base_url + model。
"""

from __future__ import annotations

from openai import OpenAI

from src.config import LLMConfig


class LLMClient:
    def __init__(self, cfg: LLMConfig):
        self.cfg = cfg
        self._client = OpenAI(base_url=cfg.base_url, api_key=cfg.api_key, timeout=120)

    def chat(self, messages: list[dict], temperature: float | None = None) -> str:
        resp = self._client.chat.completions.create(
            model=self.cfg.model,
            messages=messages,
            temperature=self.cfg.temperature if temperature is None else temperature,
            max_tokens=self.cfg.max_tokens,
        )
        return resp.choices[0].message.content or ""
