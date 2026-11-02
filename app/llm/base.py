"""LLM access layer: one narrow interface, two implementations.

The rest of the system never imports a vendor SDK. Swapping DeepSeek for Qwen
is a change of three environment variables, and the whole pipeline still runs
with no key at all because `TemplateInferencer` covers the same interface.
"""
from __future__ import annotations

import json
import re
from typing import Any

from app.core.protocols import LLMResult


class LLMError(RuntimeError):
    """Raised when the provider cannot produce usable output."""


_JSON_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


def extract_json(text: str) -> dict[str, Any]:
    """Pull a JSON object out of a model response.

    Models wrap JSON in fences, prepend "好的，" or emit trailing prose often
    enough that a plain `json.loads` is not good enough. This tries, in order:
    the raw string, a fenced block, then the outermost brace pair.
    """
    if not text or not text.strip():
        raise LLMError("模型返回空内容")

    candidates: list[str] = [text.strip()]
    fenced = _JSON_FENCE.search(text)
    if fenced:
        candidates.insert(0, fenced.group(1).strip())
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        candidates.append(text[start:end + 1])

    for candidate in candidates:
        try:
            data = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            return data
    raise LLMError(f"无法从模型输出中解析 JSON：{text[:180]!r}")
