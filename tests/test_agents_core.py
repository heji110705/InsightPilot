"""Tests for the agent layer: statistics, parsing, inferencers, nodes, pipeline.

These are the parts I wrote by hand, so they get the least forgiving tests. The
cases are chosen around the bugs that actually shipped and were fixed during the
build: a 4,202-vs-202 thousand-separator misfire, a self-rejecting derived sum,
a duplicated GROUP BY column, and `metrics[0]` swallowing every question.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from app.agents import review, stats
from app.agents.dims import (
    DimIndex,
    detect_group,
    detect_limit,
    detect_order,
    time_filter,
    wants_year_group,
)
from app.agents.inferencers import (
    LLMInferencer,
    TemplateInferencer,
    _pick_metric,
    build_inferencer,
)
from app.agents.pipeline import ask, build_deps, build_graph, cache_key
from app.core.config import PROJECT_ROOT, Settings
from app.core.models import (
    Answer,
    ChartSpec,
    Insight,
    Intent,
    MetricCard,
    ResultSet,
    SQLContext,
    TableCard,
    TurnState,
)
from app.core.protocols import LLMResult


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def rs(columns, rows) -> ResultSet:
    return ResultSet(columns=columns, rows=rows, rowcount=len(rows))


def make_settings(tmp_path) -> Settings:
    return Settings(
        db_path=PROJECT_ROOT / "data" / "warehouse.sqlite",
        semantic_dir=PROJECT_ROOT / "data" / "semantic",
        state_db_path=tmp_path / "runtime.sqlite",
        cache_enabled=False,
    )


class FakeProvider:
    """Returns canned JSON so the LLM path can be tested without a network."""

    name = "fake"

    def __init__(self, payload: str, *, boom: bool = False) -> None:
        self.payload = payload
        self.boom = boom
        self.calls: list[str] = []

    def complete(self, *, system: str, user: str, json_mode: bool = False) -> LLMResult:
        self.calls.append(system[:20])
        if self.boom:
            raise RuntimeError("network down")
        return LLMResult(text=self.payload, tokens_in=10, tokens_out=10, model="fake", ms=1)


@pytest.fixture(scope="module")
def dims() -> DimIndex:
    deps = build_deps(make_settings(Path(tempfile.mkdtemp())), force_template=True)
    return deps.dims


# --------------------------------------------------------------------------
# stats
# --------------------------------------------------------------------------
def test_fmt_uses_thousands_separator():
    assert stats.fmt(4202) == "4,202"
    assert stats.fmt(9.25) == "9.25"
    assert stats.fmt(9.2) == "9.2"
    assert stats.fmt(None) == "—"


def test_invented_number_is_caught():
    result = rs(["书籍数"], [[250]])
    bad = stats.unsupported_numbers("一共有 999 本书", stats.allowed_numbers(result))
    assert bad == ["999"]


def test_thousand_separator_is_not_split_into_two_numbers():
    """Regression: '4,202' used to be read as 4 and 202, so 202 was 'invented'."""
    result = rs(["日期", "点击数"], [["2026-10-18", 4202], ["2026-11-01", 799]])
    bad = stats.unsupported_numbers("合计 5,001，峰值 4,202", stats.allowed_numbers(result))
    assert bad == []


def test_derived_statistics_are_legitimate():
    """A sum or a mean is not in any cell but is still a true statement."""
    result = rs(["日期", "点击数"], [["2026-10-18", 4202], ["2026-11-01", 799]])
    allowed = stats.allowed_numbers(result)
    assert 5001.0 in allowed          # sum
    assert 2500.5 in allowed          # mean
    assert stats.unsupported_numbers("均值 2,500.5", allowed) == []


def test_shape_scalar():
    assert stats.infer_shape(rs(["书籍数"], [[250]])) == "scalar"


def test_shape_timeseries_accepts_two_points():
    """A two-day window is a legitimate series; requiring 3 points misclassified it."""
    assert stats.infer_shape(rs(["日期", "点击数"], [["2026-10-18", 4202], ["2026-11-01", 799]])) == "timeseries"


def test_shape_ranking_and_share():
    ranking = rs(["分类", "书籍数"], [["文学小说", 139], ["经济管理", 20], ["艺术设计", 15]])
    assert stats.infer_shape(ranking) == "ranking"
    share = rs(["分类", "占比"], [["文学小说", 55.6], ["其他", 44.4]])
    assert stats.infer_shape(share) == "share"


def test_shape_empty():
    assert stats.infer_shape(rs(["a"], [])) == "empty"


def test_chart_follows_shape():
    assert stats.select_chart("timeseries", rs(["d", "v"], [["2026-10-18", 1], ["2026-11-01", 2]])).type == "line"
    assert stats.select_chart("ranking", rs(["a", "v"], [["x", 3], ["y", 2], ["z", 1]])).type == "bar"
    assert stats.select_chart("scalar", rs(["v"], [[1]])).type == "none"


def test_repair_drops_unsupported_bullets():
    result = rs(["书籍数"], [[250]])
    insight = Insight(
        shape="scalar", headline="书籍数 为 250",
        bullets=["真实：250", "瞎编：999"], footnotes=[],
    )
    repaired = stats.repair_insight(insight, ["数字对不上"], result)
    assert "瞎编：999" not in repaired.bullets
    assert any("自检提示" in note for note in repaired.footnotes)


# --------------------------------------------------------------------------
# dims
# --------------------------------------------------------------------------
def test_chinese_numeral_limit():
    assert detect_limit("给我前五名") == 5
    assert detect_limit("前十家出版社") == 10
    assert detect_limit("排前面的八年") == 8
    assert detect_limit("top 20") == 20
    assert detect_limit("全部都要") is None


def test_group_detection_words():
    assert detect_group("哪个分类的书最多")
    assert detect_group("各个分类分别多少本")
    assert not detect_group("一共有多少本书")


def test_year_group_detection():
    assert wants_year_group("哪几年出的书最多")
    assert not wants_year_group("这本书多少钱")


def test_order_detection():
    assert detect_order("评分最高的是哪些") == "DESC"
    assert detect_order("评级最低的是哪些") == "ASC"


def test_time_filter_only_when_dates_present():
    assert time_filter("2026-10-18 那天点了多少", "clicked_on") == "clicked_on = '2026-10-18'"
    assert time_filter("10月18日到 2026-11-01 之间", "clicked_on").startswith("clicked_on = ")
    assert time_filter("最近点击量怎么样", "clicked_on") == ""


def test_longest_match_wins(dims: DimIndex):
    mentions = dims.mentions("人民文学出版社出的书有多少")
    publishers = [m.value for m in mentions if m.column == "publisher"]
    assert publishers and "出版社" in publishers[0]


# --------------------------------------------------------------------------
# inferencers
# --------------------------------------------------------------------------
def test_metric_picked_by_name_not_by_position():
    ctx = SQLContext(metrics=[
        MetricCard(key="book_cnt", name="书籍数"),
        MetricCard(key="click_cnt", name="点击数", aliases=["点击量"]),
    ])
    assert _pick_metric(ctx, "这两天的点击量有多少").key == "click_cnt"
    assert _pick_metric(ctx, "一共多少本书").key == "book_cnt"


def test_entity_sql_returns_only_two_columns(dims: DimIndex):
    engine = TemplateInferencer(dims)
    state = TurnState(
        question="最厚的十本书是哪些？",
        intent=Intent(rewritten="最厚的十本书是哪些？", entities={"limit": 10}),
        context=SQLContext(tables=[TableCard(name="v_book_overview")]),
    )
    draft = engine.sql(state)
    assert "pages AS 页数" in draft.sql
    assert "author_name" not in draft.sql, "多余的列会让结果集比问题更宽"


def test_group_column_not_injected_twice(dims: DimIndex):
    """Regression: a template that already selects the group column got it twice."""
    engine = TemplateInferencer(dims)
    metric = MetricCard(
        key="publisher_book_cnt", name="出版社书籍数",
        tables=["dim_book"],
        sql_template="SELECT publisher AS 出版社, COUNT(*) AS 书籍数 FROM dim_book {where} {group} {having} {order} {limit}",
    )
    state = TurnState(
        question="各个出版社分别有多少本书",
        intent=Intent(rewritten="各个出版社分别有多少本书", entities={"group": True}),
        context=SQLContext(tables=[TableCard(name="dim_book")], metrics=[metric]),
    )
    sql = engine.sql(state).sql
    assert sql.count("publisher AS 出版社") == 1


def test_llm_inferencer_falls_back_when_provider_fails(dims: DimIndex):
    provider = FakeProvider("", boom=True)
    engine = LLMInferencer(provider, TemplateInferencer(dims))
    state = TurnState(question="一共多少本书")
    intent = engine.intent(state)
    assert intent.kind == "query"
    assert any("降级" in note for note in intent.defaults_applied)
    assert engine.stats["intent_fallback"] == 1


def test_llm_inferencer_rejects_invented_insight_numbers(dims: DimIndex):
    provider = FakeProvider('{"headline": "一共有 9999 本书", "bullets": []}')
    engine = LLMInferencer(provider, TemplateInferencer(dims))
    state = TurnState(
        question="一共多少本书",
        result=rs(["书籍数"], [[250]]),
    )
    insight = engine.insight(state)
    assert "9999" not in insight.headline, "编造的数字必须被丢弃并回退到规则结论"
    assert engine.stats["insight_fallback"] == 1


def test_build_inferencer_without_provider_is_template(dims: DimIndex):
    assert isinstance(build_inferencer(dims, None), TemplateInferencer)


# --------------------------------------------------------------------------
# review
# --------------------------------------------------------------------------
def test_critic_blocks_on_empty_result():
    state = TurnState(question="q", result=rs(["a"], []))
    verdict = review.rule_critique(state)
    assert verdict.passed is False
    assert verdict.severity == "block"


def test_critic_blocks_on_invented_numbers():
    state = TurnState(
        question="q",
        result=rs(["书籍数"], [[250]]),
        insight=Insight(shape="scalar", headline="书籍数 为 8888", bullets=[], footnotes=["结果 1 行"]),
    )
    verdict = review.rule_critique(state)
    assert verdict.passed is False
    assert any("8888" in reason for reason in verdict.reasons)


def test_critic_warns_but_passes_on_small_sample():
    state = TurnState(
        question="q",
        result=rs(["日期", "点击数"], [["2026-10-18", 4202], ["2026-11-01", 799]]),
        insight=Insight(
            shape="timeseries", headline="合计 5,001", bullets=["最低 799"],
            footnotes=["结果 2 行"],
        ),
    )
    verdict = review.rule_critique(state)
    assert verdict.passed is True
    assert verdict.severity == "warn"


# --------------------------------------------------------------------------
# pipeline
# --------------------------------------------------------------------------
def test_graph_variants_are_all_runnable():
    settings = Settings()
    for variant in ("full", "single", "no_schema", "no_critic"):
        build_graph(settings, variant).validate()


def test_single_variant_drops_retry_and_critic():
    """The baseline must actually differ, otherwise the ablation table is fiction."""
    settings = Settings(max_retries=3)
    full = build_graph(settings, "full")
    single = build_graph(settings, "single")
    assert full._nodes["critic"].__name__ == "critic"
    assert single._nodes["critic"].__name__ == "critic_pass"
    assert full._nodes["linker"].__name__ == "linker"
    assert single._nodes["linker"].__name__ == "linker_wide"


def test_cache_key_changes_with_variant():
    a = cache_key("问题", "digest", "stub", "template:full")
    b = cache_key("问题", "digest", "stub", "template:single")
    assert a != b


def test_ask_end_to_end_and_cache(tmp_path, dims):
    settings = make_settings(tmp_path)
    settings.cache_enabled = True
    deps = build_deps(settings, force_template=True)
    answer, report = ask(
        "咱们库里现在一共收了多少本书啊？", deps=deps, use_cache=True
    )
    assert answer.status == "ok"
    assert answer.rowcount == 1
    assert answer.rows[0][0] == 250
    assert "reporter" in report.path

    second, _ = ask(
        "咱们库里现在一共收了多少本书啊？", deps=deps, use_cache=True
    )
    assert second.cached is True


def test_ask_routes_greeting_to_clarify(tmp_path):
    deps = build_deps(make_settings(tmp_path), force_template=True)
    answer, report = ask(
        "你好", deps=deps, use_cache=False
    )
    assert answer.status == "clarify"
    assert "问数助手" in answer.message
    assert report.path == ["intake", "clarify"]


def test_ask_rejects_empty_question():
    with pytest.raises(ValueError):
        ask("   ")
