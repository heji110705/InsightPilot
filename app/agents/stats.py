"""Deterministic statistics and chart selection.

Rule of the house: **numbers are computed here, never by the model.** The model
may phrase a sentence, but every numeral in that sentence is re-checked against
the result set by `unsupported_numbers()` and the sentence is dropped if it
invents one. Hallucinated figures in a data product are worse than a plain
sentence, so the fallback is always the version computed in this module.
"""
from __future__ import annotations

import re
from typing import Any

from app.core.models import ChartSpec, Insight, InsightShape, ResultSet

_NUMBER = re.compile(r"-?\d+(?:\.\d+)?")
_DATE_LIKE = re.compile(r"^\d{4}-\d{2}-\d{2}$|^\d{4}-\d{2}$|年|月|日|周")
_THOUSANDS = re.compile(r"(?<=\d),(?=\d{3})")


def _strip_thousands(text: str) -> str:
    """`4,202` must be read as one number, not as 4 and 202."""
    return _THOUSANDS.sub("", text or "")
_CN_MONTH = ("年", "月", "日")


def as_number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip().rstrip("%"))
        except ValueError:
            return None
    return None


def fmt(value: Any, digits: int = 2) -> str:
    """Render a cell for prose: ints stay ints, floats get trimmed decimals."""
    number = as_number(value)
    if number is None:
        return "—" if value is None else str(value)
    if abs(number - round(number)) < 1e-9:
        return f"{int(round(number)):,}"
    return f"{round(number, digits):,.2f}".rstrip("0").rstrip(".")


def collect_numbers(result: ResultSet) -> set[float]:
    """Every number that legitimately appears in the answer.

    String cells are scanned too: a date like `2026-10-18` is not a number, but
    it is rendered verbatim in the prose, so `2026` must not be treated as an
    invented figure.
    """
    numbers: set[float] = set()
    for row in result.rows:
        for cell in row:
            value = as_number(cell)
            if value is not None:
                numbers.add(round(value, 4))
            elif isinstance(cell, str):
                for token in _NUMBER.findall(_strip_thousands(cell)):
                    try:
                        numbers.add(round(float(token), 4))
                    except ValueError:
                        continue
    numbers.add(float(result.rowcount))
    numbers.update({float(len(result.rows)), 0.0})
    return numbers


def derived_numbers(result: ResultSet) -> set[float]:
    """Numbers a reader can legitimately re-derive from the result set.

    A total, a mean, a first-to-last delta are all honest statements about the
    data even though no single cell contains them. Without this set the critic
    would reject its own summary — which is exactly what happened before this
    function existed.
    """
    out: set[float] = set()
    for index in range(len(result.columns)):
        values = [as_number(row[index]) for row in result.rows if index < len(row)]
        numbers = [v for v in values if v is not None]
        if len(numbers) < 2:
            continue
        candidates = (
            sum(numbers),
            sum(numbers) / len(numbers),
            max(numbers),
            min(numbers),
            numbers[0] - numbers[-1],
            numbers[-1] - numbers[0],
            max(numbers) - min(numbers),
        )
        for value in candidates:
            out.update({round(value, 4), round(value, 2), round(value)})
    return out


def allowed_numbers(result: ResultSet) -> set[float]:
    """The full set the critic checks against: raw cells plus re-derivable stats."""
    return collect_numbers(result) | derived_numbers(result)


def unsupported_numbers(text: str, allowed: set[float], question: str = "") -> list[str]:
    """Numbers in `text` that cannot be traced back to the result set.

    Small integers (<= 10) and anything present in the question are tolerated:
    "前 5 名" and "2026 年" are references, not claims.
    """
    question_numbers = {round(float(x), 4) for x in _NUMBER.findall(_strip_thousands(question or ""))}
    loose = {round(n * 100, 4) for n in allowed if abs(n) <= 1.0}
    permitted = allowed | loose | question_numbers
    bad: list[str] = []
    for raw in _NUMBER.findall(_strip_thousands(text or "")):
        try:
            value = round(float(raw), 4)
        except ValueError:
            continue
        if abs(value) <= 10:
            continue
        if value in permitted:
            continue
        if any(abs(value - candidate) < 0.051 for candidate in permitted):
            continue
        bad.append(raw)
    return bad


def infer_shape(result: ResultSet) -> InsightShape:
    if not result.rows:
        return "empty"
    if len(result.columns) == 1 and len(result.rows) == 1:
        return "scalar"
    if len(result.columns) < 2:
        return "detail"

    first_col_values = [str(row[0]) for row in result.rows if row and row[0] is not None]
    if len(result.rows) >= 2 and first_col_values and all(
        _DATE_LIKE.search(value) for value in first_col_values[:5]
    ):
        return "timeseries"

    second = [as_number(row[1]) for row in result.rows if len(row) > 1]
    if second and all(value is not None for value in second):
        total = sum(v for v in second if v is not None)
        if 2 <= len(result.rows) <= 6 and total > 0 and all(0 <= v <= 100 for v in second):
            if abs(total - 100.0) < 1.5:
                return "share"
        if len(result.rows) >= 3:
            return "ranking"
    return "detail"


def _xy(columns: list[str], rows: list[list[Any]]) -> tuple[str, str, list[dict[str, Any]]]:
    x_name = columns[0] if columns else "x"
    y_name = columns[1] if len(columns) > 1 else "y"
    points: list[dict[str, Any]] = []
    for row in rows:
        if len(row) < 2:
            continue
        value = as_number(row[1])
        if value is None:
            continue
        points.append({"label": str(row[0]), "value": round(value, 4)})
    return x_name, y_name, points


def select_chart(shape: InsightShape, result: ResultSet) -> ChartSpec:
    """Chart choice follows the question the data answers, not aesthetics."""
    if shape in ("empty", "scalar", "detail") or not result.rows:
        return ChartSpec(type="none", reason="该结果集不适合画图，表格更准确")

    x_name, y_name, points = _xy(result.columns, result.rows)
    if not points:
        return ChartSpec(type="none", reason="无可用数值列")

    if shape == "timeseries":
        return ChartSpec(
            type="line", title=f"{y_name}随时间变化", x_field=x_name, y_field=y_name,
            points=points, reason="时间维度用折线，斜率可比、缺失日不误导",
        )
    if shape == "share":
        return ChartSpec(
            type="pie", title=f"{y_name}构成", x_field=x_name, y_field=y_name,
            points=points, reason="占比且类别不超过 6 类，饼图可读",
        )
    top = sorted(points, key=lambda p: p["value"], reverse=True)[:12]
    return ChartSpec(
        type="bar", title=f"{y_name}排名", x_field=x_name, y_field=y_name,
        points=top, reason="排序比较用条形，长度比角度更准",
    )


def compute_insight(result: ResultSet, question: str = "") -> Insight:
    """Build the guaranteed-correct version of the answer narrative."""
    shape = infer_shape(result)
    chart = select_chart(shape, result)
    footnotes = [f"结果 {result.rowcount} 行"]
    if result.truncated:
        footnotes.append("结果已按行数上限截断，结论仅基于返回的前若干行")
    if shape == "empty":
        return Insight(
            shape="empty",
            headline="该条件下没有查到数据",
            bullets=["建议放宽筛选条件，或确认字段取值是否与库内一致"],
            footnotes=footnotes, chart=None,
        )

    columns = result.columns
    rows = result.rows
    headlines: list[str] = []
    bullets: list[str] = []

    if shape == "scalar":
        headlines.append(f"{columns[0]} 为 {fmt(rows[0][0])}")

    elif shape == "ranking":
        x_name, y_name, points = _xy(columns, rows)
        ranked = sorted(points, key=lambda p: p["value"], reverse=True)
        headlines.append(f"{ranked[0]['label']} 的 {y_name} 最高，为 {fmt(ranked[0]['value'])}")
        for rank, point in enumerate(ranked[:3], start=1):
            bullets.append(f"第 {rank} 名：{point['label']}，{y_name} {fmt(point['value'])}")
        if len(ranked) >= 2:
            gap = ranked[0]["value"] - ranked[1]["value"]
            bullets.append(f"第一名与第二名相差 {fmt(gap)}")

    elif shape == "timeseries":
        x_name, y_name, points = _xy(columns, rows)
        values = [p["value"] for p in points]
        peak = max(points, key=lambda p: p["value"])
        low = min(points, key=lambda p: p["value"])
        headlines.append(
            f"{y_name}在这 {len(points)} 个时间点合计 {fmt(sum(values))}，"
            f"峰值出现在 {peak['label']}（{fmt(peak['value'])}）"
        )
        bullets.append(f"最低点为 {low['label']}，{y_name} {fmt(low['value'])}")
        if len(values) >= 2:
            bullets.append(
                f"首尾变化：{fmt(values[0])} → {fmt(values[-1])}"
                f"（{fmt(values[-1] - values[0], 1)}）"
            )
        bullets.append(f"时间点均值 {fmt(sum(values) / len(values))}")

    elif shape == "share":
        x_name, y_name, points = _xy(columns, rows)
        ranked = sorted(points, key=lambda p: p["value"], reverse=True)
        headlines.append(f"{ranked[0]['label']} 占 {fmt(ranked[0]['value'])}%，为最大项")
        for point in ranked[1:3]:
            bullets.append(f"{point['label']} 占 {fmt(point['value'])}%")

    else:  # detail
        headlines.append(f"共返回 {result.rowcount} 行，前几行见表格")
        if rows:
            bullets.append("首行：" + "，".join(fmt(cell) for cell in rows[0][:4]))

    if result.rowcount < 3 and shape in ("ranking", "share", "timeseries"):
        footnotes.append("样本量偏小（不足 3 行），差异不宜过度解读")
        bullets.append("样本量不足，排名稳定性有限")

    return Insight(
        shape=shape, headline=headlines[0], bullets=bullets,
        footnotes=footnotes, chart=chart,
    )


def repair_insight(insight: Insight, reasons: list[str], result: ResultSet) -> Insight:
    """Drop claims the critic could not verify, keep the rest."""
    allowed = allowed_numbers(result)
    kept: list[str] = []
    dropped = 0
    for line in insight.bullets:
        if unsupported_numbers(line, allowed):
            dropped += 1
            continue
        kept.append(line)
    footnotes = list(insight.footnotes)
    if dropped:
        footnotes.append(f"已移除 {dropped} 条无法由结果集支撑的表述")
    for reason in reasons:
        if reason not in footnotes:
            footnotes.append(f"自检提示：{reason}")
    headline = insight.headline
    if unsupported_numbers(headline, allowed):
        headline = compute_insight(result).headline
    return insight.model_copy(update={"headline": headline, "bullets": kept, "footnotes": footnotes})


def render_table(result: ResultSet, max_rows: int = 12) -> str:
    """Compact markdown-ish table for prompt context."""
    head = " | ".join(result.columns)
    lines = [head, "-" * len(head)]
    for row in result.rows[:max_rows]:
        lines.append(" | ".join(fmt(cell) for cell in row))
    if result.rowcount > max_rows:
        lines.append(f"... 共 {result.rowcount} 行")
    return "\n".join(lines)
