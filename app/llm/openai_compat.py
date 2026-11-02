"""OpenAI-compatible chat-completions provider.

DeepSeek, Qwen (dashscope compat mode), Zhipu, Moonshot and OpenAI all expose
the same `/chat/completions` shape, so one implementation covers them. Written
against httpx directly rather than the openai SDK: the surface used here is two
fields, and a raw call makes the request/response visible in the trace.
"""
from __future__ import annotations

import logging
import time

import httpx

from app.core.protocols import LLMResult
from app.llm.base import LLMError

logger = logging.getLogger(__name__)


class OpenAICompatProvider:
    name = "openai-compat"

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        *,
        timeout_s: int = 60,
        max_tokens: int = 1200,
        temperature: float = 0.0,
        max_attempts: int = 2,
    ) -> None:
        if not api_key:
            raise LLMError("未配置 API Key（IP_LLM_API_KEY）")
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout_s = timeout_s
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.max_attempts = max_attempts

    def complete(self, *, system: str, user: str, json_mode: bool = False) -> LLMResult:
        payload: dict[str, object] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}

        last_error: Exception | None = None
        for attempt in range(self.max_attempts):
            started = time.perf_counter()
            try:
                data = self._post(payload)
            except httpx.HTTPStatusError as exc:
                status = exc.response.status_code
                # Some providers reject response_format outright. Dropping it
                # and retrying costs one call and keeps the provider matrix open.
                if json_mode and status in (400, 422) and "response_format" in payload:
                    logger.info("provider rejected json_mode, retrying without it")
                    payload.pop("response_format")
                    last_error = exc
                    continue
                if status in (429, 500, 502, 503, 504) and attempt + 1 < self.max_attempts:
                    last_error = exc
                    time.sleep(0.8 * (attempt + 1))
                    continue
                raise LLMError(f"模型接口返回 {status}：{exc.response.text[:200]}") from exc
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_error = exc
                if attempt + 1 < self.max_attempts:
                    time.sleep(0.8 * (attempt + 1))
                    continue
                raise LLMError(f"模型接口不可达：{exc}") from exc

            ms = int((time.perf_counter() - started) * 1000)
            return self._parse(data, ms)

        raise LLMError(f"模型调用失败：{last_error}")

    # ------------------------------------------------------------------
    def _post(self, payload: dict[str, object]) -> dict:
        with httpx.Client(timeout=self.timeout_s) as client:
            response = client.post(
                f"{self.base_url}/chat/completions",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
            response.raise_for_status()
            return response.json()

    def _parse(self, data: dict, ms: int) -> LLMResult:
        try:
            text = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError(f"模型响应结构异常：{str(data)[:200]}") from exc
        usage = data.get("usage") or {}
        return LLMResult(
            text=text or "",
            tokens_in=int(usage.get("prompt_tokens") or 0),
            tokens_out=int(usage.get("completion_tokens") or 0),
            model=str(data.get("model") or self.model),
            ms=ms,
        )
