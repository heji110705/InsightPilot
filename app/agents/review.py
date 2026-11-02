"""Rule-based self-check (the Critic's brain).

Deliberately rule-based in both modes. A model asked "is this answer right?"
tends to agree with itself; three narrow mechanical checks catch the failures
that actually happen: invented numbers, tiny samples, and a missing caveat.
Making it rule-based also keeps the `no_critic` ablation meaningful.
"""
from __future__ import annotations

import re

from app.agents.stats import allowed_numbers, unsupported_numbers
from app.core.models import TurnState, Verdict

_TABLE_ALIAS = re.compile(r"\bAS\s+(\w+)\b", re.IGNORECASE)


def rule_critique(state: TurnState) -> Verdict:
    if state.result is None:
        return Verdict(passed=False, severity="block", reasons=["没有拿到结果集，无法给出结论"])

    result = state.result
    if result.rowcount == 0:
        return Verdict(
            passed=False, severity="block",
            reasons=["查询返回 0 行，任何结论都不成立；应如实说明无数据，而不是解释成零"],
        )

    reasons: list[str] = []
    severity = "info"
    insight = state.insight
    if insight is None:
        return Verdict(passed=False, severity="block", reasons=["缺少结论对象"])

    allowed = allowed_numbers(result)
    narrative = " ".join([insight.headline, *insight.bullets])
    invented = unsupported_numbers(narrative, allowed, state.question)
    if invented:
        reasons.append(f"结论里出现了结果集没有的数字：{'、'.join(invented[:3])}")
        severity = "block"

    if result.rowcount < 3 and insight.shape in ("ranking", "share", "timeseries"):
        reasons.append(f"样本量只有 {result.rowcount} 行，排名/趋势类结论不稳")
        severity = "block" if severity == "block" else "warn"

    if not insight.footnotes:
        reasons.append("缺少口径说明，读者无法判断这个数是怎么算的")
        severity = "block" if severity == "block" else "warn"

    if result.truncated:
        reasons.append("结果被行数上限截断，结论只覆盖返回的部分")
        severity = "block" if severity == "block" else "warn"

    return Verdict(passed=severity != "block", severity=severity, reasons=reasons)
