"""语义层与真实数仓的一致性测试。

为什么需要：卡片上写"12 个取值"而不列出来，模型就会自己编一个（实测：问「四大名著」
时写 `WHERE category = '四大名著'`，返回 0 行，看起来像"数据里没有"）。

所以这里守的是一条硬约束：**数仓里出现的低基数维度取值，必须在卡片里逐字出现**。
数仓加了新分类而卡片没跟上时，这条测试会失败——这比"模型答错了才发现"早得多。
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from app.semantic.loader import load_semantic

SEMANTIC_DIR = Path("data/semantic")
WAREHOUSE = Path("data/warehouse.sqlite")


def _card_text(table: str) -> str:
    """把一张表卡片摊平成文本：meaning + 字段说明 + caveats。"""
    bundle = load_semantic(SEMANTIC_DIR)
    card = next(t for t in bundle.tables if t.name == table)
    return "\n".join([card.meaning, *card.columns, *card.caveats])


def _distinct(column: str) -> list[str]:
    with sqlite3.connect(WAREHOUSE) as conn:
        rows = conn.execute(
            f"SELECT DISTINCT {column} FROM dim_book WHERE {column} IS NOT NULL"
        )
        return [r[0] for r in rows if str(r[0]).strip()]


@pytest.mark.skipif(not WAREHOUSE.exists(), reason="数仓不存在")
def test_every_category_value_is_documented() -> None:
    text = _card_text("dim_book")
    missing = [v for v in _distinct("category") if v not in text]
    assert not missing, f"dim_book 卡片漏了这些 category 取值：{missing}"


@pytest.mark.skipif(not WAREHOUSE.exists(), reason="数仓不存在")
def test_category_card_warns_against_inventing_values() -> None:
    """卡片必须明确说"俗称不是取值"，否则模型会拿题材俗称去当等值条件。"""
    text = _card_text("dim_book")
    assert "俗称" in text, "category 的 caveat 应说明「四大名著」这类是俗称、不是取值"


def test_semantic_digest_is_content_sensitive(tmp_path: Path) -> None:
    """语义层指纹：同样内容稳定，改一个字就变。

    它进 SQL 缓存键，所以"改了卡片、缓存没失效"这种事不该发生。
    """
    import shutil

    first = load_semantic(SEMANTIC_DIR).digest
    assert first == load_semantic(SEMANTIC_DIR).digest
    assert len(first) == 12

    copy = tmp_path / "semantic"
    shutil.copytree(SEMANTIC_DIR, copy)
    target = copy / "tables.yml"
    target.write_text(target.read_text(encoding="utf-8") + "\n# tweak\n", encoding="utf-8")
    assert load_semantic(copy).digest != first


def test_cache_key_includes_semantic_digest() -> None:
    from app.agents.pipeline import cache_key

    base = cache_key("四大名著有什么", "schema", "api", "llm:full", "aaaaaaaaaaaa")
    changed = cache_key("四大名著有什么", "schema", "api", "llm:full", "bbbbbbbbbbbb")
    assert base != changed, "语义层变了，缓存键必须跟着变，否则会一直吐旧 SQL"
