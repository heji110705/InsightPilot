"""输出约定（semantic/defaults.yml）的加载与渲染测试。

这一层的价值不在"多了一个配置文件"，而在于**行数 / 小数位 / 最小样本量
不再由模型每次临场发挥**。所以测试要盯住三件事：
1. 缺文件、缺字段时行为可预期（不能因为少写一段就让 SQL 崩）；
2. 深合并而不是整段替换（只改 limits 不能把 format 抹掉）；
3. 渲染出来的提示词里是**具体数值**，不是"适量""合理"这种软措辞。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.agents.prompts import render_context
from app.core.models import SQLContext
from app.semantic.loader import FALLBACK_DEFAULTS, _merge_defaults, load_semantic


def test_missing_file_falls_back() -> None:
    """目录里没有 defaults.yml 时，整套兜底值仍然可用。

    用的是 tests/fixtures/semantic —— 那份夹具刻意不含 defaults.yml，
    正好覆盖"老语义层目录没有这个文件"的升级场景。
    """
    bundle = load_semantic(Path("tests/fixtures/semantic"))
    assert bundle.defaults["limits"]["ranking"] == FALLBACK_DEFAULTS["limits"]["ranking"]
    assert bundle.defaults["stats"]["min_group_size"] >= 1


def test_empty_doc_returns_full_fallback() -> None:
    merged = _merge_defaults({})
    assert merged == FALLBACK_DEFAULTS


def test_partial_doc_merges_instead_of_replacing() -> None:
    """只写 limits 时，format / stats 必须保留兜底值——这是整段替换最容易犯的错。"""
    merged = _merge_defaults({"limits": {"ranking": 20}})
    assert merged["limits"]["ranking"] == 20
    assert merged["limits"]["single"] == FALLBACK_DEFAULTS["limits"]["single"]
    assert merged["format"] == FALLBACK_DEFAULTS["format"]
    assert merged["stats"] == FALLBACK_DEFAULTS["stats"]


@pytest.mark.parametrize("bad_value", ["10 行", None, True, [10], {"n": 10}])
def test_non_numeric_values_are_ignored(bad_value) -> None:
    """非数值不能被塞进 SQL（"10 行" 这种写法在 YAML 里很常见）。"""
    merged = _merge_defaults({"limits": {"ranking": bad_value}})
    assert merged["limits"]["ranking"] == FALLBACK_DEFAULTS["limits"]["ranking"]


def test_float_values_are_accepted() -> None:
    merged = _merge_defaults({"format": {"percent_scale": 1.0}})
    assert merged["format"]["percent_scale"] == 1.0


def test_render_contains_concrete_numbers() -> None:
    ctx = SQLContext(defaults=_merge_defaults({"limits": {"ranking": 7}}))
    text = render_context(ctx)
    assert "输出约定" in text
    assert "LIMIT 7" in text          # 用户改的值必须出现在提示词里
    assert "2 位小数" in text
    assert "少于 5 的组" in text


def test_render_is_empty_without_defaults() -> None:
    """没有约定时不渲染空段落，避免给模型一段无内容的标题。"""
    assert "输出约定" not in render_context(SQLContext())


def test_single_row_rule_is_explicit() -> None:
    """单数问题给 1 行这条必须写死在提示词里——它是 T6 那一批题的主要失分点。"""
    text = render_context(SQLContext(defaults=_merge_defaults({})))
    assert "单数" in text
    assert "LIMIT 1" in text
