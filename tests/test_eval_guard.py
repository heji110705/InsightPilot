"""降级计量与模式一致性的测试。

这两件事都指向同一个失败模式：**报告里写的模式和实际跑的路径不一致**。
它们不会让测试变红，只会让数字变成假的，所以必须有断言守住。
"""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from app.agents.pipeline import build_deps
from app.core.config import load_settings
from app.eval.runner import _degradation


class _FakeInferencer:
    def __init__(self, stats: dict[str, int] | None) -> None:
        self.name = "llm"
        if stats is not None:
            self.stats = stats


class _FakeDeps:
    def __init__(self, inferencer: object) -> None:
        self.inferencer = inferencer


# ---------------------------------------------------------------------------
# _degradation
# ---------------------------------------------------------------------------
def test_degradation_flags_majority_sql_fallback() -> None:
    deps = _FakeDeps(_FakeInferencer({"sql_fallback": 55, "intent_fallback": 3}))
    out = _degradation(deps, total=60)  # type: ignore[arg-type]
    assert out["degraded"] is True
    assert "55/60" in out["degraded_reason"]
    assert out["fallback"]["sql_fallback"] == 55


def test_degradation_accepts_occasional_fallback() -> None:
    """少量回退是正常的（模型偶尔返回不合规 JSON），不该一票否决。"""
    deps = _FakeDeps(_FakeInferencer({"sql_fallback": 2, "intent_fallback": 1}))
    out = _degradation(deps, total=60)  # type: ignore[arg-type]
    assert out["degraded"] is False
    assert out["degraded_reason"] == ""


def test_degradation_flags_total_intent_fallback() -> None:
    """意图解析全量回退，说明模型这一侧根本没通，必须判降级。"""
    deps = _FakeDeps(_FakeInferencer({"sql_fallback": 0, "intent_fallback": 60}))
    out = _degradation(deps, total=60)  # type: ignore[arg-type]
    assert out["degraded"] is True
    assert "意图" in out["degraded_reason"]


def test_degradation_without_counters_is_neutral() -> None:
    """模板推理器没有 stats，不能因此被判成降级。"""
    out = _degradation(_FakeDeps(_FakeInferencer(None)), total=60)  # type: ignore[arg-type]
    assert out["degraded"] is False
    assert out["fallback"] is None


def test_degradation_handles_zero_total() -> None:
    out = _degradation(_FakeDeps(_FakeInferencer({"sql_fallback": 0})), total=0)  # type: ignore[arg-type]
    assert out["degraded"] is False


# ---------------------------------------------------------------------------
# 强制模板模式必须同时降级 settings
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def live_settings():
    """拿真实配置，但把状态库指向临时目录。"""
    import tempfile

    base = load_settings()
    state = Path(tempfile.mkdtemp()) / "runtime.sqlite"
    return replace(base, state_db_path=state)


def test_force_template_downgrades_settings(live_settings) -> None:
    """即使 .env 配了 api，force_template 下对外必须报 stub + 无模型名。"""
    api_settings = replace(
        live_settings, llm_mode="api", llm_api_key="sk-dummy-key-for-test"
    )
    deps = build_deps(settings=api_settings, force_template=True)
    try:
        assert deps.settings.effective_mode == "stub"
        assert deps.settings.llm_api_key == ""
        assert deps.provider is None
        assert deps.inferencer.name == "template"
    finally:
        deps.repo.close()


def test_api_mode_keeps_settings_intact(live_settings) -> None:
    """反向断言：不强制模板时不能误降级，否则真实模型永远跑不起来。"""
    api_settings = replace(
        live_settings, llm_mode="api", llm_api_key="sk-dummy-key-for-test"
    )
    deps = build_deps(settings=api_settings, force_template=False)
    try:
        assert deps.settings.effective_mode == "api"
        assert deps.settings.llm_api_key == "sk-dummy-key-for-test"
        assert deps.inferencer.name == "llm"
    finally:
        deps.repo.close()


# ---------------------------------------------------------------------------
# 报告文案必须随模式切换，不能自相矛盾
# ---------------------------------------------------------------------------
def _metrics(mode: str, inferencer: str) -> dict:
    base = dict(
        variant="full",
        mode=mode,
        inferencer=inferencer,
        limit=None,
        total=60,
        ex_count=42,
        ex=0.7,
        pass_at_1_count=38,
        pass_at_1=0.633,
        avg_attempts=1.12,
        attempts_gt1=9,
        p50_ms=5200,
        p95_ms=9100,
        p95_method="nearest-rank",
        cache_hits=0,
        fallback={"sql_fallback": 0, "intent_fallback": 0, "insight_fallback": 2},
        degraded=False,
        degraded_reason="",
        types={
            t: {"ex": 0.7, "ex_count": 7, "total": 10}
            for t in ("T1", "T2", "T3", "T4", "T5", "T6")
        },
        failures={"failed": 1, "rejected": 0, "clarify": 1, "wrong": 16},
    )
    return {"metrics": base, "items": []}


def test_report_api_mode_never_claims_template_baseline() -> None:
    """api 模式下报告说"这不是 LLM 模式"就是自相矛盾，必须被守住。"""
    from app.eval.report import render_report

    text = render_report({"full": _metrics("api", "llm")}, command="demo")
    assert "这不是 LLM 模式" not in text
    assert "规则基线" not in text
    assert "真实模型模式" in text


def test_report_stub_mode_still_flags_baseline() -> None:
    """反向断言：模板模式下必须继续声明不是模型效果，防止把基线当成绩。"""
    from app.eval.report import render_report

    text = render_report({"full": _metrics("stub", "template")}, command="demo")
    assert "这不是 LLM 模式" in text
    assert "真实模型模式" not in text


def test_report_api_mode_skips_template_root_cause_analysis() -> None:
    """模型模式即使低分，也不能把根因写成"模板推理器只取 Top-1"。"""
    from app.eval.report import render_report

    entry = _metrics("api", "llm")
    entry["metrics"]["ex"] = 0.2
    entry["metrics"]["ex_count"] = 12
    text = render_report({"full": entry}, command="demo")
    assert "_metric_sql" not in text
    assert "TemplateInferencer" not in text
    assert "人工复核" in text


# ---------------------------------------------------------------------------
# 章节编号：改过一次章序，把「4.1」留在了第 5 章下面
# ---------------------------------------------------------------------------
def _headings(text: str) -> list[tuple[int, str]]:
    found: list[tuple[int, str]] = []
    for line in text.splitlines():
        if line.startswith("### "):
            found.append((3, line[4:].strip()))
        elif line.startswith("## "):
            found.append((2, line[3:].strip()))
    return found


def _report_variants() -> list[dict]:
    """三种会走到不同分支的输入：常规分、api 低分、模板低分。"""
    normal = {"full": _metrics("api", "llm")}

    low_api = _metrics("api", "llm")
    low_api["metrics"]["ex"] = 0.2
    low_api["metrics"]["ex_count"] = 12

    return [normal, {"full": low_api}, {"full": _metrics("stub", "template")}]


def test_report_chapters_are_sequential() -> None:
    """章号必须是 1..N 连号，中间不能跳号或回头。"""
    from app.eval.report import render_report

    for results in _report_variants():
        chapters = [int(title.split(".", 1)[0]) for level, title in _headings(render_report(results, command="demo")) if level == 2]
        assert chapters == list(range(1, len(chapters) + 1)), chapters


def test_report_subsection_numbers_follow_their_chapter() -> None:
    """小节编号的前缀必须等于所在章号——否则读者会以为引用的节不存在。"""
    from app.eval.report import render_report

    for results in _report_variants():
        chapter = 0
        for level, title in _headings(render_report(results, command="demo")):
            if level == 2:
                chapter = int(title.split(".", 1)[0])
                continue
            prefix = title.split(" ", 1)[0]
            assert prefix.startswith(f"{chapter}."), f"第 {chapter} 章下出现 {prefix}：{title}"

