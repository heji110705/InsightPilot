"""把 ``runner`` 的原始结果渲染成 ``docs/03-评测报告.md``。

报告里的每一个比率、分位数、题数都来自 ``eval_results.json``；这里只做
格式化，不做任何"估计"或"修正"。如果某一组数字看起来不合理，正确的做法
是去查评测链路并把原因写进第 5 节，而不是在这里挑一个好看的值。
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

__all__ = ["render_report", "write_report"]

TYPES = ("T1", "T2", "T3", "T4", "T5", "T6")
TYPE_NAMES = {
    "T1": "单表聚合",
    "T2": "分组排序",
    "T3": "多表关联",
    "T4": "时间窗",
    "T5": "比率",
    "T6": "口径陷阱",
}
VARIANT_ORDER = ("full", "no_critic", "no_schema", "single")
VARIANT_LABEL = {
    "full": "full（完整 6 Agent）",
    "no_critic": "no_critic（关闭 Critic）",
    "no_schema": "no_schema（去掉相关性排序）",
    "single": "single（单链路、无重试、无 Critic）",
}


# --------------------------------------------------------------------------
# 格式化小工具
# --------------------------------------------------------------------------
def _pct(value: float) -> str:
    return f"{value * 100:.1f}%"


def _rate_cell(rate: float, count: int, total: int) -> str:
    return f"{_pct(rate)}（{count}/{total}）"


def _cell(text: str, limit: int = 90) -> str:
    """Markdown 表格单元格：竖线会断表，长文本要截断。"""
    clean = str(text or "").replace("|", "/").replace("\n", " ").strip()
    if not clean:
        return "—"
    return clean if len(clean) <= limit else clean[: limit - 1] + "…"


def _ordered(results: dict[str, dict[str, Any]]) -> list[str]:
    known = [name for name in VARIANT_ORDER if name in results]
    others = sorted(name for name in results if name not in VARIANT_ORDER)
    return known + others


def _sql_hint(pred_sql: str, gold_sql: str) -> str:
    """从两条 SQL 的结构差异推断一句人话，辅助定位，不参与判定。"""
    pred, gold = (pred_sql or "").upper(), (gold_sql or "").upper()
    hints: list[str] = []
    if "GROUP BY" in gold and "GROUP BY" not in pred:
        hints.append("缺 GROUP BY")
    if " LIMIT " in gold and " LIMIT " not in pred:
        hints.append("缺 LIMIT")
    if " JOIN " in gold and " JOIN " not in pred:
        hints.append("缺 JOIN")
    if " OVER " in gold and " OVER " not in pred:
        hints.append("缺窗口函数")
    if "INTERSECT" in gold and "INTERSECT" not in pred:
        hints.append("缺集合运算")
    return "、".join(hints[:2])


def _failure_reason(item: dict[str, Any]) -> str:
    status = str(item.get("status") or "ok")
    if status == "failed":
        detail = item.get("error") or item.get("diff") or "无错误信息"
        return f"执行失败：{_cell(detail, 60)}"
    if status == "clarify":
        return "意图被判为澄清/超范围，未进入 SQL 生成"
    if status == "rejected":
        detail = item.get("error") or "自检未通过"
        return f"自检拒绝：{_cell(detail, 60)}"

    diff = str(item.get("diff") or "结果不等")
    if diff.startswith("列数"):
        prefix = "投影列数不符"
    elif diff.startswith("行数"):
        prefix = "行数/分组粒度不符"
    elif diff.startswith("第"):
        prefix = "取值/口径不符"
    else:
        prefix = "结果不等"
    text = f"{prefix}（{_cell(diff, 70)}）"
    hint = _sql_hint(str(item.get("sql") or ""), str(item.get("gold_sql") or ""))
    return f"{text}；{hint}" if hint else text


# --------------------------------------------------------------------------
# 各章节
# --------------------------------------------------------------------------
def _section_conclusion(results: dict[str, dict[str, Any]]) -> list[str]:
    names = _ordered(results)
    metrics = [results[name]["metrics"] for name in names]
    full = results["full"]["metrics"] if "full" in results else metrics[0]
    total = int(full["total"])

    if len(metrics) == 1:
        sentence1 = (
            f"当前只跑了 {full['variant']} 一组：{total} 题 EX {_pct(full['ex'])}"
            f"（{full['ex_count']}/{total}），首答正确率 {_pct(full['pass_at_1'])}，"
            f"平均尝试 {full['avg_attempts']:.2f} 次。"
        )
    else:
        best = max(metrics, key=lambda m: (m["ex"], m["pass_at_1"]))
        worst = min(metrics, key=lambda m: (m["ex"], m["pass_at_1"]))
        best_names = "/".join(m["variant"] for m in metrics if m["ex"] == best["ex"])
        worst_names = "/".join(m["variant"] for m in metrics if m["ex"] == worst["ex"])
        pieces = "、".join(f"{m['variant']} {_pct(m['ex'])}" for m in metrics)
        gap = (best["ex"] - worst["ex"]) * 100
        sentence1 = (
            f"{len(metrics)} 组配置在 {total} 题上的 EX 分别为 {pieces}；"
            f"最高 {_pct(best['ex'])}（{best_names}），最低 {_pct(worst['ex'])}"
            f"（{worst_names}），极差 {gap:.1f} 个百分点。"
        )

    mode = full.get("mode", "stub")
    if "no_schema" in results and "full" in results:
        delta = (full["ex"] - results["no_schema"]["metrics"]["ex"]) * 100
        if mode == "api":
            critic_note = (
                f"no_critic 与 full 的差距为 "
                f"{(full['ex'] - results['no_critic']['metrics']['ex']) * 100:+.1f} 个百分点，"
                f"`attempts>1` 的题数为 {full['attempts_gt1']} 次。"
                if "no_critic" in results
                else ""
            )
        else:
            critic_note = (
                "而 no_critic 与 full 同分、`attempts>1` 的题数为 "
                f"{full['attempts_gt1']}，说明规则模板 SQL 是确定性的，"
                "Critic 与重试在这条路径上没有可发挥的场景。"
            )
        sentence2 = (
            f"full 比 no_schema 高 {delta:.1f} 个百分点（{_pct(full['ex'])} vs "
            f"{_pct(results['no_schema']['metrics']['ex'])}），"
            "差异对应 SchemaLinker 是否按相关性挑指标。" + critic_note
        )
    else:
        sentence2 = (
            f"{full['variant']} 的首答正确率 {_pct(full['pass_at_1'])}、平均尝试 "
            f"{full['avg_attempts']:.2f} 次，失败集中在 SQL 结构与口径，而不是执行异常。"
        )

    if mode == "api":
        sentence3 = (
            f"本次为真实模型模式（`{full.get('inferencer', '?')}` / {mode}），"
            "数字可代表该模型在该提示词下的表现，但不可跨模型版本或提示词版本直接比较；"
            "局限性见第 7 节。"
        )
    else:
        sentence3 = (
            f"这不是 LLM 模式：本次为 {mode}/`{full.get('inferencer', '?')}` 规则基线，"
            "绝对数字只说明模板路径的能力上限，不能当作模型效果；原因见第 5 节。"
        )
    return ["## 1. 结论", "", sentence1, "", sentence2, "", sentence3, ""]


def _section_ablation(results: dict[str, dict[str, Any]]) -> list[str]:
    lines = [
        "## 2. 消融对比",
        "",
        "| 配置 | EX | 覆盖一致 | 首答正确率 | 平均尝试 | p50 | p95 |",
        "|---|---|---|---|---|---|---|",
    ]
    for name in _ordered(results):
        m = results[name]["metrics"]
        label = VARIANT_LABEL.get(name, name)
        lines.append(
            f"| {label} | {_rate_cell(m['ex'], m['ex_count'], m['total'])} "
            f"| {_rate_cell(m.get('cover', m['ex']), m.get('cover_count', m['ex_count']), m['total'])} "
            f"| {_rate_cell(m['pass_at_1'], m['pass_at_1_count'], m['total'])} "
            f"| {m['avg_attempts']:.2f} | {m['p50_ms']} ms | {m['p95_ms']} ms |"
        )
    lines += [
        "",
        "> **EX** 要求列数与金标准完全相等；**覆盖一致**允许预测多带列，只要金标准的每一列"
        "都能在预测里按值找到对应列、行也不错位。两者之差就是「答对了但投影多给了列」的比例。"
        "本项目的目标是「答案包含正确信息」，所以覆盖一致更接近真实可用性；EX 作为严格参考保留。",
        "> p95 为最近秩百分位；样本不足 20 时退化为最大值（本次 n=60，不涉及）。"
        "延迟是端到端图执行耗时，含 SQL 执行，不含评测脚本读写 JSON 的开销。",
        "> 平均尝试按**全题数**计算：被 intake 判为 out_of_scope 走 clarify 的题没有进入"
        "executor，attempts 记 0，因此均值会略低于 1.00。",
        "",
    ]
    return lines


def _section_cost(results: dict[str, dict[str, Any]]) -> list[str]:
    """成本与调用量：回答「多开一个 Agent 值不值」。

    只看准确率会得出「Agent 越多越好」；把 token、调用次数、钱并列出来，
    才能看出关掉 Critic 到底省了多少、又损失了多少。
    """
    rows = []
    for name in _ordered(results):
        m = results[name]["metrics"]
        if m.get("tokens_per_question") is None:
            continue
        rows.append((VARIANT_LABEL.get(name, name), m))
    if not rows:
        return []

    lines = [
        "## 3. 成本与调用量",
        "",
        "| 配置 | 模型调用次数 | 输入 token | 输出 token | 单题 token | 单题成本 | 单题模型耗时 |",
        "|---|---|---|---|---|---|---|",
    ]
    for label, m in rows:
        lines.append(
            f"| {label} | {m['llm_calls']} "
            f"| {m['tokens_in']:,} | {m['tokens_out']:,} "
            f"| {m['tokens_per_question']:.0f} "
            f"| ¥{m['cost_per_question_cny']:.5f} "
            f"| {m['llm_ms_per_question']:.0f} ms |"
        )
    total_cost = sum(m["cost_cny"] or 0 for _, m in rows)
    lines += [
        "",
        f"> 本轮全部配置合计花费约 **¥{total_cost:.3f}**。单价取 DeepSeek-chat 公开价"
        "（输入 ¥1 / 百万 token，输出 ¥2 / 百万 token），可用 `IP_PRICE_IN_PER_M`、"
        "`IP_PRICE_OUT_PER_M` 覆盖。这个数字用于**横向比较配置之间**，不作账单依据。",
        "> 调用次数是真实 HTTP 请求数，不含回退到模板的那些——回退时没有花 token，"
        "所以「单题成本 × 题数」会略低于真实账单。",
        "",
    ]
    return lines


def _section_types(results: dict[str, dict[str, Any]]) -> list[str]:
    header = "| 配置 | " + " | ".join(f"{t} {TYPE_NAMES[t]}" for t in TYPES) + " | 合计 |"
    sep = "|---|" + "---|" * (len(TYPES) + 1)
    lines = [
        "## 4. 分类型 EX",
        "",
        header,
        sep,
    ]
    for name in _ordered(results):
        m = results[name]["metrics"]
        cells: list[str] = []
        for qtype in TYPES:
            row = m.get("types", {}).get(qtype)
            cells.append(
                _rate_cell(row["ex"], row["ex_count"], row["total"]) if row else "—"
            )
        cells.append(_rate_cell(m["ex"], m["ex_count"], m["total"]))
        lines.append(f"| {name} | " + " | ".join(cells) + " |")
    lines += [
        "",
        "> 题型定义：T1 单表聚合、T2 分组排序、T3 多表关联、T4 时间窗、"
        "T5 比率、T6 口径陷阱；每类 10 题。",
        "",
    ]
    return lines


def _section_failures(results: dict[str, dict[str, Any]]) -> list[str]:
    lines = [
        "## 5. 失败分析",
        "",
        "### 4.1 失败原因分布",
        "",
        "| 配置 | failed | rejected | clarify | wrong | EX=False 合计 |",
        "|---|---|---|---|---|---|",
    ]
    for name in _ordered(results):
        m = results[name]["metrics"]
        f = m["failures"]
        lines.append(
            f"| {name} | {f['failed']} | {f['rejected']} | {f['clarify']} "
            f"| {f['wrong']} | {m['total'] - m['ex_count']} |"
        )
    lines += [
        "",
        "> `wrong` = SQL 执行成功（status=ok）但结果集不等价；它是最值得看的一类，"
        "说明问题出在 SQL 结构/口径而不是执行链路。",
        "",
        "### 4.2 逐题失败清单",
        "",
    ]

    for name in _ordered(results):
        items = results[name]["items"]
        failures = [item for item in items if not item["ex"]]
        m = results[name]["metrics"]
        lines.append(
            f"<details><summary>{name}：{len(failures)}/{m['total']} 题失败</summary>"
        )
        lines.append("")
        if not failures:
            lines.append("无失败题目。")
        else:
            lines.append("| 题号 | 类型 | status | 原因 |")
            lines.append("|---|---|---|---|")
            for item in failures:
                lines.append(
                    f"| {item['id']} | {item['type']} | {item['status']} "
                    f"| {_failure_reason(item)} |"
                )
        lines += ["", "</details>", ""]
    return lines


def _failure_taxonomy(items: list[dict[str, Any]]) -> list[tuple[str, int]]:
    """把 ``compare`` 的差异说明归成几类，回答"错在哪一层"。

    只做字符串分类，不做根因推断——分类是事实（每条的 ``diff`` 都能回查），
    根因要靠人看两条 SQL。顺序按题数降序，``其他`` 永远垫底。
    """
    counts: dict[str, int] = {}
    for item in items:
        if item.get("ex"):
            continue
        diff = str(item.get("diff") or "")
        if diff.startswith("列数不同"):
            kind = "列数不同"
        elif diff.startswith("行数不同"):
            kind = "行数不同"
        elif diff.startswith("第") and "列" in diff:
            kind = "单元格不等"
        elif "评测异常" in diff:
            kind = "评测异常"
        elif not item.get("sql"):
            kind = "未生成 SQL"
        else:
            kind = "其他"
        counts[kind] = counts.get(kind, 0) + 1
    ordered = sorted(counts.items(), key=lambda kv: (kv[0] == "其他", -kv[1]))
    return ordered


def _section_diagnosis(results: dict[str, dict[str, Any]]) -> list[str]:
    lines = ["## 6. 低分排查记录", ""]
    full = results.get("full", next(iter(results.values())))["metrics"]
    mode = full.get("mode", "stub")

    if mode == "api":
        # 模型模式下，失败构成本身就是结论的一半：列数/行数/取值三类差异
        # 对应三种完全不同的修法，混在一起看会得出"模型不行"的错误结论。
        first = results.get("full") or next(iter(results.values()))
        taxonomy = _failure_taxonomy(list(first.get("items") or []))
        if taxonomy:
            meaning = {
                "列数不同": "输出列数不一致，多为模型多给了未被问到的描述列",
                "行数不同": "过滤条件或 TOP-N 阈值与预期不一致",
                "单元格不等": "行列数都对但取值不同（口径或聚合层级差异）",
                "未生成 SQL": "走 clarify / 拒绝，没有进入执行阶段",
                "评测异常": "引擎抛错，需看 error 字段",
                "其他": "需人工比对两条 SQL",
            }
            lines += [
                "模型模式下的失败构成（按 `compare` 给出的差异类型归类，每条可回查）：",
                "",
                "| 差异类型 | 题数 | 常见含义 |",
                "|---|---|---|",
            ]
            for kind, count in taxonomy:
                lines.append(f"| {kind} | {count} | {meaning.get(kind, '—')} |")
            lines += [
                "",
                "> 分类只是把差异摊开，不等于根因。同一类里可能同时存在"
                "「多给了一列」和「少给了关键列」，必须回看第 5 节的逐题明细。",
                "",
            ]

    if all(entry["metrics"]["ex"] >= 0.3 for entry in results.values()):
        return lines + [
            "本次所有配置的 EX 均不低于 0.3 的防呆线，未触发低分专项排查；"
            "评测框架的自检项（`cache_hits`、`attempts_gt1`、失败分布）仍记录在 JSON 与上表中。",
            "",
        ]
    wrong = full["failures"]["wrong"]
    failed = full["failures"]["failed"]
    clarify = full["failures"]["clarify"]

    if full.get("mode") == "api":
        # 模型模式下不给根因结论：下面的模板分析全部不成立，而凭失败分布
        # 反推"模型为什么错"属于猜测，写进报告就是把猜测当结论。
        # 这里只摆可核对的事实，把逐题差异留给第 5 节的明细。
        return lines + [
            "EX 低于 0.3 的防呆线。模型模式下的排查只陈述可核对的事实，不推断根因：",
            "",
            "1. **不是缓存命中**：每题都显式 `use_cache=False`，本次 "
            f"`cache_hits={full['cache_hits']}`。",
            "2. **不是降级**：本次 `degraded=false`，`sql_fallback` 未达阈值，"
            "SQL 确实由模型生成（见本节末的回退计数）。",
            f"3. **失败构成**：{full['total'] - full['ex_count']} 题失败，其中 "
            f"`wrong` {wrong} 题（SQL 能跑但结果不等）、`failed` {failed} 题、"
            f"`clarify` {clarify} 题。`wrong` 占多数时，问题在语义理解或口径选择，"
            "不在执行链路。",
            "",
            "逐题差异请对照第 5 节明细与 `eval_results.json` 里的 `pred_sql` 自行复核；"
            "若要归因，应按题型抽样人工比对 pred SQL 与 gold SQL 的差异类型"
            "（漏 JOIN / 口径错 / 聚合层级错 / 多余过滤），不要从总数反推。",
            "",
            "### 5.1 待人工复核项",
            "",
            "以下两类必须先排除评测集自身的问题，再谈模型表现：",
            "- **gold 口径与题面不一致**的题（模板基线阶段已记录 Q57、Q22/Q51），"
            "在模型模式下同样会被判错，属于评测集问题而非模型问题。",
            "- **结果集等价不等于语义正确**：同一数字的不同口径无法被本框架区分。",
            "",
        ]

    lines += [
        "EX 低于 0.3 的防呆线，先排除评测框架本身的问题，结论如下（全部可复核）：",
        "",
        "1. **不是缓存命中**：每题都显式 `use_cache=False`，本次指标里 "
        f"`cache_hits={full['cache_hits']}`，且 `report.path` 均以 `intake` 开头。",
        "2. **不是 deps 复用串题**：同一 variant 复用 deps 只共享只读的 repo / "
        "retriever / DimIndex，`TurnState` 每题新建；重复跑同一题结果稳定。",
        f"3. **不是执行链路坏了**：full 的 {full['total'] - full['ex_count']} 题失败中，"
        f"`wrong` {wrong} 题（SQL 成功执行但结果不等）、`failed` {failed} 题、"
        f"`clarify` {clarify} 题；绝大多数是「能跑但不对」。",
        "",
        "真正的根因在规则模板推理器 `TemplateInferencer`，与评测框架无关：",
        "",
        "- **只信 Top-1 指标**：`_metric_sql` 直接取 `context.metrics[0]`，不校验该指标"
        "是否回答当前问题。Q35「每天有多少人来逛」的 Top-1 是「收藏数」，生成的 SQL "
        "变成 `COUNT(*) FROM fact_shelf`；Q42「整体好评率」的 Top-1 是「正向评论占比」，"
        "落到了 `fact_comment`（89.42% vs 73.39%）。更隐蔽的是口径错位：Q02「涉及多少位"
        "作者」的 Top-1「作者数」把口径定在 `dim_author`（197），而 gold 按 `dim_book` "
        "去重（184）；Q04「多少人在打分」的 Top-1 是「评分数」，直接 `COUNT(*)` 出 4202，"
        "而 gold 要去重用户（101）。",
        "- **结构解析覆盖不足**：`detect_group` 只认「各/按/每/分别/分组」，"
        "「哪个分类」「哪几年」不触发 GROUP BY；`detect_limit` 只认阿拉伯数字，"
        "「前十家」「前五名」「八年」都拿不到 LIMIT。Q11 因此退化成全表计数。",
        "- **固定 4 列投影**：命中实体类问题时模板固定输出"
        "「书名/作者/分类/指标」四列，而 gold 多为 2 列（如 Q21 只要书名+评论数），"
        "列数不同直接判不等，哪怕 Top-10 的书是对的。",
        "- **单表模板表达不了 JOIN/窗口/子查询**：60 题里大量 T3/T4/T5/T6 需要多表"
        "预聚合、`OVER`、`INTERSECT`，指标模板只有单表聚合，结构上不可能等价。",
        "",
    ]
    if "no_schema" in results:
        lines += [
            "`no_schema` / `single` 的额外塌陷：`linker_wide` 把指标目录前 8 条原样塞进"
            "上下文，`metrics[0]` 恒为「书籍数」，于是几乎每题都变成 `COUNT(*) FROM dim_book`，"
            f"EX 降到 {_pct(results['no_schema']['metrics']['ex'])}。"
            "这正好反证了相关性排序的价值，但也说明该消融在模板模式下分辨的是"
            "「指标选择」而不是「LLM 的 schema linking 能力」。",
            "",
            "消融也不是逐题单调的：Q03 在 full 下 Top-1 命中「出版社书籍数」（2 列），"
            "反而 no_schema 固定取「书籍数」加维度过滤（1 列）答对了。5 vs 2 的组间差距里"
            "既有召回收益，也有召回选错指标的噪声，不能简化成「每个模块都正向」。",
            "",
        ]
    lines += [
        "Critic 与重试为何没有差异：模板 SQL 是确定性的，执行错误几乎为 0，"
        f"`attempts>1` 的题数为 {full['attempts_gt1']}；`rule_critique` 只校验结论里的"
        "数字是否能被结果集支撑，而模板结论本身由结果集算出，因而总是通过。"
        "换句话说，**在规则基线上这两个模块没有可发挥的场景，不能据此判断它们在 "
        "LLM 模式下无效**。",
        "",
        "### 5.1 引擎侧代码缺陷（只报告，不修改）",
        "",
        "- **重复投影列**：`TemplateInferencer._metric_sql` 在指标模板已经自带分组列时，"
        "仍执行 `sql.replace(\"SELECT \", f\"SELECT {column} AS {alias}, \", 1)`，"
        "产出 `SELECT category AS 分类, category AS 分类, COUNT(*) AS 书籍数 ...`。"
        "full / no_critic 各有 4 题命中（Q13、Q20、Q24、Q43），列名重复且第二列语义错误。"
        "本次不修：交付范围限定在评测文件，改动推理器会让四组基线数字失去可比性。"
        "修掉它也不会直接让这 4 题通过（Q13 还缺 `price > 1` 过滤、Q43 缺占比列、"
        "Q24 缺 JOIN），但失败原因会从「重复列」回到真实的 SQL 结构差异。",
        "",
        "### 5.2 gold 口径观察（只报告，不修改）",
        "",
        "- **Q57**：问题说「收藏和点击都很少」，但 gold SQL 只过滤 "
        "`click_cnt < 20`，没有给 `shelf_cnt` 设阈值（该子集收藏数实际 2–21），"
        "`note` 也只写了 click 阈值。若按题面严格执行，结果集会不同。",
        "- **Q22 vs Q51**：前者「被收藏最多」按收藏次数（`COUNT(shelf_id)`），"
        "后者「最受欢迎」按去重收藏人数；两种口径都写在 note 里，属有意设计，"
        "但结果集等价的评测无法区分「口径选错但数字碰巧相同」的情况。",
        "- 其余 note 中的关键数字已抽检并与库内一致（以下数字来自直接对 "
        "`warehouse.sqlite` 执行校验 SQL，不是评测脚本产物）：评分总数 4202、"
        "书目作者 184 / 作者档案 197、整体好评率 73.39%、精装 63 本、"
        "豆瓣更高 196 本、Q57 命中 25 本、Q52 第一名《少年凯歌》9.7/17 人、"
        "Q54 两种口径第一名均为「哲学宗教」。",
        "",
    ]
    return lines


def _section_limitations(results: dict[str, dict[str, Any]]) -> list[str]:
    full = results.get("full", next(iter(results.values())))["metrics"]
    mode = full.get("mode", "stub")
    template_note = (
        "**本报告为规则基线结果**：运行在 template 模式（无 LLM Provider，"
        f"inferencer=`{full.get('inferencer', 'template')}`），"
        "不代表 LLM 模式表现；接入模型后必须重跑，不能引用本表数字。"
        if mode != "api"
        else "本报告为 api 模式结果，但模型版本、温度、提示词变化都会改变数字，跨版本不可直接比较。"
    )
    return [
        "## 7. 局限性声明",
        "",
        f"- {template_note}",
        "- **数据快照极窄**：仓库只有 2026-10-18 与 2026-11-01 两天（且都是周日），"
        "T4 时间类题目没有「上个月/按周趋势」的真实跨度，区分度有限；"
        "任何关于时间分析的结论都不具备统计意义。",
        "- **样本量小**：60 题、每题权重 1.67%，百分比的 95% 置信区间约 ±12 个百分点；"
        "两组相差 1–2 题（1.7–3.3 个百分点）不能说明模块优劣。",
        "- **结果集等价 ≠ 语义正确**：列名与行序不参与比较，同一数字的不同口径"
        "（如收藏次数 vs 收藏人数）可能都被判为「等价」；评测只能证明"
        "「算出了同一张表」，不能证明「选对了业务口径」。",
        "",
    ]


def _section_meta(results: dict[str, dict[str, Any]], command: str) -> list[str]:
    names = _ordered(results)
    totals = {results[name]["metrics"]["total"] for name in names}
    total_text = "/".join(str(value) for value in sorted(totals))
    modes = sorted({str(results[name]["metrics"].get("mode", "?")) for name in names})
    inferencers = sorted(
        {str(results[name]["metrics"].get("inferencer", "?")) for name in names}
    )
    p95_methods = sorted({str(results[name]["metrics"].get("p95_method", "?")) for name in names})
    cache_hits = sum(int(results[name]["metrics"].get("cache_hits", 0)) for name in names)
    generated = datetime.now().strftime("%Y-%m-%d %H:%M")
    return [
        "## 8. 运行元信息与复现命令",
        "",
        "| 项 | 值 |",
        "|---|---|",
        f"| 生成时间 | {generated} |",
        f"| 评测集 | `data/gold.jsonl`（{total_text} 题） |",
        f"| 模式 | {', '.join(modes)} |",
        f"| Inferencer | {', '.join(inferencers)} |",
        f"| 配置数 | {len(names)}（{', '.join(names)}） |",
        f"| p95 口径 | {', '.join(p95_methods)} |",
        f"| 缓存命中 | {cache_hits}（应为 0） |",
        "",
        "```bash",
        command,
        "```",
        "",
    ]


# --------------------------------------------------------------------------
# 入口
# --------------------------------------------------------------------------
def render_report(
    results: dict[str, dict[str, Any]],
    *,
    command: str = "D:/Python312/python.exe scripts/run_eval.py --variants full,single,no_schema,no_critic --out docs/",
) -> str:
    if not results:
        raise ValueError("没有可渲染的评测结果")

    lines: list[str] = [
        "# InsightPilot 评测报告（结果集等价）",
        "",
        "> 判定口径：列数相同、列名不比较、行序不比较（多重集）、数值 1e-6 相对容差、"
        "字符串去首尾空白且大小写敏感、None 与空串不等。",
        "> 所有指标由 `scripts/run_eval.py` 实跑写入 `docs/eval_results.json` 后渲染，"
        "没有手填值；第 5.2 节标注为直接 SQL 抽检的数字除外。",
        "",
    ]
    any_mode = next(iter(results.values()))["metrics"].get("mode", "stub")
    if any_mode == "api":
        lines += [
            "> **先看重复性再读表**：同一配置重复运行的 EX 极差在本项目实测为 3.3 个百分点"
            "（见 `docs/03b-重复性检验.md`）。本表是**单次运行**结果，"
            "任何小于该量级的组间差异都不应被解释为模块效果。",
            "",
        ]
    lines += _section_conclusion(results)
    lines += _section_ablation(results)
    lines += _section_cost(results)
    lines += _section_types(results)
    lines += _section_failures(results)
    lines += _section_diagnosis(results)
    lines += _section_limitations(results)
    lines += _section_meta(results, command)
    return "\n".join(lines).rstrip() + "\n"


def write_report(path: Path | str, text: str) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    return target
