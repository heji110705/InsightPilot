"""关键词判据：DocuMind 可答题的判分口径。

**组间 AND、组内 OR**：`[["28%"], ["订单"]]` 要求两样都出现。写成「任一命中即算对」
会让分数虚高——模型学会答半边就能拿满分。

来源是 DocuMind 的 `evalutil.hit`，这里是它的独立副本而不是 import（本目录要能整体
搬进任意仓库）。语义由 `tests/test_core.py::TestHit` 钉住，改一条就红。
"""

from __future__ import annotations

__all__ = ["hit"]


def hit(text: str, groups: list[list[str]]) -> bool:
    if not groups:
        return True
    return all(any(alt in text for alt in g) for g in groups)
