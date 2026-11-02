"""语义层加载器与检索器的行为测试。

夹具一律用 `tests/fixtures/semantic/`，不依赖 `data/semantic/`（那份由
另一个 Agent 并行编写，随时可能还在变）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.semantic.loader import SemanticBundle, load_semantic
from app.semantic.retriever import (
    SemanticRetriever,
    est_tokens,
    example_text,
    metric_text,
    table_text,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "semantic"
BUDGET = 2600

HIT_REASON_PREFIXES = ("name_exact", "alias:", "synonym:", "bm25:", "join:", "metric:")


@pytest.fixture(scope="module")
def bundle() -> SemanticBundle:
    return load_semantic(FIXTURES)


@pytest.fixture(scope="module")
def retriever(bundle: SemanticBundle) -> SemanticRetriever:
    return SemanticRetriever(bundle)


# --------------------------------------------------------------------------
# loader
# --------------------------------------------------------------------------
def test_loader_reads_all_four_files(bundle: SemanticBundle):
    assert [t.name for t in bundle.tables] == [
        "dim_book", "dim_author", "fact_rating", "fact_comment",
    ]
    assert len(bundle.metrics) == 5
    assert len(bundle.fewshots) == 5
    assert bundle.synonyms["评分"] == ["打分", "分数", "评级"]
    assert "fact_rating.book_id = dim_book.book_id" in bundle.join_hints


def test_loader_folds_column_meaning_into_text(bundle: SemanticBundle):
    """TableCard 没有列级 synonyms 字段，列含义被折叠进字符串。"""
    book = next(t for t in bundle.tables if t.name == "dim_book")
    rating = next(c for c in book.columns if c.startswith("rating_avg"))
    assert rating.startswith("rating_avg")
    assert "评分" in rating


def test_loader_tolerates_missing_files_and_fields(tmp_path: Path):
    """缺 synonyms.yml / fewshots.yml、缺 caveats / joins 都不许抛异常。"""
    (tmp_path / "tables.yml").write_text(
        "tables:\n"
        "  - name: dim_x\n"
        "    meaning: 最小可用表\n"
        "    columns:\n"
        "      - {name: id, type: INTEGER}\n"
        "      - 裸字符串列\n",
        encoding="utf-8",
    )
    (tmp_path / "metrics.yml").write_text(
        "metrics:\n  - key: x_cnt\n    name: 行数\n",
        encoding="utf-8",
    )
    loaded = load_semantic(tmp_path)
    assert [t.name for t in loaded.tables] == ["dim_x"]
    assert loaded.tables[0].caveats == []
    assert loaded.tables[0].columns == ["id", "裸字符串列"]
    assert loaded.metrics[0].aliases == []
    assert loaded.metrics[0].sql_template == ""      # 缺字段补空串，不炸
    assert loaded.fewshots == []
    assert loaded.synonyms == {}
    assert loaded.join_hints == []


def test_loader_on_empty_directory(tmp_path: Path):
    """空目录：召回为空，但输出约定必须有兜底。

    早先这里断言整个 bundle 等于空对象，加了 defaults 之后那条断言就不再成立——
    而且不成立是对的：没有语义层文件时，系统仍然应该知道"默认返回 10 行"，
    否则连退化成模板模式都拼不出一条完整 SQL。
    """
    loaded = load_semantic(tmp_path)
    assert loaded.tables == []
    assert loaded.metrics == []
    assert loaded.fewshots == []
    assert loaded.synonyms == {}
    assert loaded.defaults["limits"]["ranking"] >= 1


# --------------------------------------------------------------------------
# 三路召回 + 结构加权
# --------------------------------------------------------------------------
def test_exact_name_hit_ranks_first(retriever: SemanticRetriever):
    """指标名原文出现在问题里 -> 排第一，且 hit_reason 为 name_exact。"""
    ctx = retriever.search("评分人数最多的书", budget=BUDGET)
    assert ctx.metrics[0].key == "rating_user_cnt"
    assert ctx.metrics[0].hit_reason == "name_exact"
    assert ctx.tables[0].name == "dim_book"


def test_alias_hit_is_reported(retriever: SemanticRetriever):
    """别名命中：问题里写"多少本书"，命中的是别名而不是指标名。"""
    ctx = retriever.search("一共有多少本书", budget=BUDGET)
    top = ctx.metrics[0]
    assert top.key == "book_cnt"
    assert top.hit_reason == "alias:多少本书"
    assert top.score > 0


def test_synonym_expansion_rescues_metric(retriever: SemanticRetriever):
    """问题用同义词"打分"，指标里写的是"评分"，靠 synonyms 扩展补上。"""
    ctx = retriever.search("这本书打分高吗", budget=BUDGET)
    keys = [m.key for m in ctx.metrics]
    assert "avg_rating" in keys
    hit = next(m for m in ctx.metrics if m.key == "avg_rating")
    assert hit.hit_reason.startswith("synonym:")


def test_join_two_hop_brings_counterpart_table(retriever: SemanticRetriever):
    """Top1 表的 JOIN 对手表被强制带回，否则跨表问题拼不出 JOIN。"""
    ctx = retriever.search("一共有多少本书", budget=BUDGET)
    names = [t.name for t in ctx.tables]
    assert ctx.tables[0].name == "dim_book"
    assert "fact_rating" in names
    joined = next(t for t in ctx.tables if t.name == "fact_rating")
    assert joined.hit_reason == "join:dim_book"
    assert joined.score == pytest.approx(0.01)


def test_join_two_hop_does_not_duplicate(retriever: SemanticRetriever):
    ctx = retriever.search("一共有多少本书", budget=BUDGET)
    names = [t.name for t in ctx.tables]
    assert len(names) == len(set(names))


def test_every_item_has_readable_hit_reason(retriever: SemanticRetriever):
    ctx = retriever.search("哪个分类的平均评分最高", budget=BUDGET)
    for card in [*ctx.tables, *ctx.metrics]:
        assert card.hit_reason
        assert card.hit_reason.startswith(HIT_REASON_PREFIXES)


# --------------------------------------------------------------------------
# 预算裁剪
# --------------------------------------------------------------------------
def test_budget_trim_drops_low_score_items(retriever: SemanticRetriever):
    ctx = retriever.search("书籍维度表里有多少本书", budget=60)
    assert ctx.dropped > 0
    assert [t.name for t in ctx.tables] == ["dim_book"]     # Top1 表无条件保底
    assert ctx.token_est > 0


def test_examples_keep_at_least_two(retriever: SemanticRetriever):
    """即使预算被表吃光，范例也要留两条（超出预算也认）。"""
    ctx = retriever.search("书籍维度表里有多少本书", budget=60)
    assert len(ctx.examples) >= 2
    assert len(ctx.examples) <= 5


def test_generous_budget_keeps_everything(retriever: SemanticRetriever):
    ctx = retriever.search("哪个分类的平均评分最高", budget=BUDGET)
    assert ctx.dropped == 0
    assert ctx.token_est <= BUDGET


def test_token_est_matches_kept_content(retriever: SemanticRetriever):
    """token_est 必须等于"最终留下来的东西"的估算之和，多算少算都会误导预算调参。"""
    ctx = retriever.search("哪个分类的平均评分最高", budget=BUDGET)
    expected = sum(est_tokens(table_text(t)) for t in ctx.tables)
    expected += sum(est_tokens(metric_text(m)) for m in ctx.metrics)
    expected += sum(est_tokens(example_text(e)) for e in ctx.examples)
    if ctx.join_hints:
        expected += est_tokens(" ".join(ctx.join_hints))
    assert ctx.token_est == expected


def test_token_est_grows_with_budget_items(retriever: SemanticRetriever):
    small = retriever.search("书籍维度表里有多少本书", budget=60)
    large = retriever.search("书籍维度表里有多少本书", budget=BUDGET)
    assert large.token_est > small.token_est


# --------------------------------------------------------------------------
# hints
# --------------------------------------------------------------------------
def test_hints_join_the_query(retriever: SemanticRetriever):
    """上游 Intent 抽出的实体（hints）要并入 query，能改变召回集合。"""
    plain = retriever.search("各出版社的书价", budget=BUDGET)
    hinted = retriever.search("各出版社的书价", budget=BUDGET, hints=["评论数"])
    assert "comment_cnt" not in [m.key for m in plain.metrics]
    assert "comment_cnt" in [m.key for m in hinted.metrics]
    assert hinted.token_est > plain.token_est


# --------------------------------------------------------------------------
# 只读接口
# --------------------------------------------------------------------------
def test_metric_lookup(retriever: SemanticRetriever):
    card = retriever.metric("avg_rating")
    assert card is not None and card.name == "平均评分"
    assert retriever.metric("not_exists") is None


def test_metrics_list_is_complete(retriever: SemanticRetriever):
    assert [m.key for m in retriever.metrics()] == [
        "book_cnt", "avg_rating", "rating_user_cnt", "comment_cnt", "avg_price",
    ]


def test_all_tables_returns_full_catalogue(retriever: SemanticRetriever):
    """`all_tables` 是给 prompt 拼全量 schema 用的，不受检索预算影响。"""
    names = [t.name for t in retriever.all_tables()]
    assert len(names) == 4
    assert "fact_comment" in names


def test_search_does_not_mutate_bundle(bundle: SemanticBundle, retriever: SemanticRetriever):
    retriever.search("评分人数最多的书", budget=BUDGET)
    assert all(t.score == 0.0 and t.hit_reason == "" for t in bundle.tables)
    assert all(m.score == 0.0 for m in bundle.metrics)


def test_search_survives_empty_bundle():
    """空语义层（真实目录可能还没生成）不能让链路 500。"""
    ctx = SemanticRetriever(SemanticBundle()).search("随便问点什么", budget=100)
    assert ctx.tables == [] and ctx.metrics == [] and ctx.examples == []
    assert ctx.dropped == 0
