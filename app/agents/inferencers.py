"""Two implementations of the same `Inferencer` seam.

`TemplateInferencer` answers with dimension linking plus the metric catalog's
`sql_template` — no network, fully deterministic. It is not a stub: it is the
**baseline** the LLM has to beat, and the reason the ablation table means
something. `LLMInferencer` wraps the same seam and falls back to it on any
failure, so a bad model response degrades instead of crashing.
"""
from __future__ import annotations

import logging

from app.agents import prompts
from app.agents.dims import (
    DATE_COLUMNS,
    DimIndex,
    Mention,
    clean_question,
    detect_group,
    detect_kind,
    detect_limit,
    detect_order,
    detect_vague,
    primary_table,
    time_filter,
    wants_year_group,
)
from app.agents.review import rule_critique
from app.agents.stats import allowed_numbers, compute_insight, render_table, unsupported_numbers
from app.core.models import (
    Ambiguity,
    Insight,
    Intent,
    MetricCard,
    SQLDraft,
    TurnState,
    Verdict,
)
from app.core.protocols import LLMProvider
from app.llm.base import LLMError, extract_json

logger = logging.getLogger(__name__)

GROUP_ALIAS = {
    "category": "分类", "publisher": "出版社", "author_name": "作者",
    "status": "状态", "event": "事件", "clicked_on": "日期",
    "rated_on": "日期", "created_on": "日期", "pub_year": "出版年份",
    "binding": "装帧",
}
DEFAULT_GROUP = {
    "dim_book": "category", "v_book_overview": "category",
    "fact_click": "clicked_on", "fact_rating": "rated_on", "fact_comment": "created_on",
}

# 顺序有意义：「评分人数」必须先于「评分」匹配，否则 TOP-N 会被理解成按均分排。
_ENTITY_MEASURES: tuple[tuple[tuple[str, ...], str, str], ...] = (
    (("评分人数", "多少人评", "打分人数", "评价人数"), "rating_cnt", "评分人数"),
    (("豆瓣评分", "豆瓣分", "豆瓣的评价"), "douban_rating", "豆瓣评分"),
    (("评论数", "评论最多", "书评最多", "被讨论"), "comment_cnt", "评论数"),
    (("收藏数", "收藏最多", "加入书架最多", "书架最多", "被收藏"), "shelf_cnt", "收藏数"),
    (("点击量", "点击数", "点击最多", "被点"), "click_cnt", "点击数"),
    (("页数", "最厚", "最薄"), "pages", "页数"),
    (("最贵", "价格最高", "定价最高", "卖多少钱", "定价"), "price", "价格"),
    (("评分最高", "评分最好", "分最高", "分数最高"), "rating_avg", "站内评分"),
)
_ENTITY_HINTS = (
    "哪些书", "哪几本", "哪本书", "前几本", "排名", "榜单", "所有书", "书名", "列一下",
    "哪些", "哪本", "排行", "前十", "榜上",
)


def _esc(value: str) -> str:
    return str(value).replace("'", "''")


def _is_followup(question: str) -> bool:
    return (question or "").strip().startswith(("再", "那", "这", "换", "按", "只", "不", "还有"))


def _entity_measure(question: str) -> tuple[str, str] | None:
    for keywords, column, alias in _ENTITY_MEASURES:
        if any(word in (question or "") for word in keywords):
            return column, alias
    return None


def _wants_entities(question: str) -> bool:
    return any(hint in (question or "") for hint in _ENTITY_HINTS)


def _pick_metric(context, question: str) -> MetricCard | None:
    """Prefer the metric whose name or alias literally appears in the question.

    Blindly taking `metrics[0]` was the single biggest source of wrong answers in
    the first eval run: every question became "count books" because that metric
    ranks first in the catalog.
    """
    if context is None or not context.metrics:
        return None
    best: MetricCard | None = None
    best_len = 0
    for metric in context.metrics:
        for term in [metric.name, *metric.aliases]:
            term = (term or "").strip()
            if len(term) >= 2 and term in question and len(term) > best_len:
                best, best_len = metric, len(term)
    return best or context.metrics[0]


class TemplateInferencer:
    """Deterministic baseline: rules + metric templates, no model involved."""

    name = "template"

    def __init__(self, dims: DimIndex) -> None:
        self.dims = dims

    # -- intent ---------------------------------------------------------
    def intent(self, state: TurnState) -> Intent:
        question = state.question or ""
        kind = detect_kind(question)
        rewritten = clean_question(question)
        if state.history and _is_followup(question):
            last_question = str(state.history[-1].get("question", "") or "")
            if last_question:
                rewritten = f"{last_question} {rewritten}".strip()

        mentions = self.dims.mentions(rewritten)
        entities: dict[str, object] = {}
        limit = detect_limit(question)
        if limit:
            entities["limit"] = limit
        for mention in mentions:
            entities[mention.column] = mention.value
        order = detect_order(question)
        if order:
            entities["order"] = order
        if detect_group(question):
            entities["group"] = True

        ambiguities = [
            Ambiguity(
                term=term,
                options=options,
                chosen=options[0],
                reason="规则模式默认取第一个口径；换成模型模式会给出理由",
            )
            for term, options in detect_vague(question)
        ]
        defaults = [f"「{a.term}」按「{a.chosen}」理解" for a in ambiguities]

        return Intent(
            kind=kind,
            rewritten=rewritten,
            entities=entities,
            ambiguities=ambiguities,
            defaults_applied=defaults,
            needs_clarify=False,
        )

    # -- sql ------------------------------------------------------------
    def sql(self, state: TurnState, feedback: str = "") -> SQLDraft:
        intent = state.intent
        question = (intent.rewritten if intent and intent.rewritten else state.question) or ""
        mentions = self.dims.mentions(question)
        measure = _entity_measure(question)

        if measure and _wants_entities(question):
            return self._entity_sql(question, measure, mentions, intent)

        metric = _pick_metric(state.context, question)
        if metric is not None:
            draft = self._metric_sql(metric, question, mentions, intent)
            if draft is not None:
                return draft
        if measure is not None:
            return self._entity_sql(question, measure, mentions, intent)
        return self._detail_sql(question, mentions, intent)

    def _metric_sql(
        self, metric: MetricCard, question: str, mentions: list[Mention], intent: Intent | None,
    ) -> SQLDraft | None:
        template = (metric.sql_template or "").strip()
        if not template or "{" not in template:
            return None
        table = primary_table(list(metric.tables) + [template]) or "dim_book"

        filters: list[str] = []
        time_clause = time_filter(question, DATE_COLUMNS.get(table))
        if time_clause:
            filters.append(time_clause)

        group_mention: Mention | None = None
        if detect_group(question):
            group_mention = next(
                (m for m in mentions if m.table == table and m.column in GROUP_ALIAS), None
            )
            if group_mention is None and table == "dim_book" and wants_year_group(question):
                group_mention = Mention(table=table, column="pub_year", value="")
            if group_mention is None and DEFAULT_GROUP.get(table):
                group_mention = Mention(table=table, column=DEFAULT_GROUP[table], value="")

        for mention in mentions:
            if group_mention is not None and mention.column == group_mention.column:
                continue
            if mention.table != table:
                continue
            filters.append(f"{mention.column} = '{_esc(mention.value)}'")

        if group_mention is not None and group_mention.column == "pub_year":
            filters.append("pub_year IS NOT NULL")
        where = f"WHERE {' AND '.join(filters)}" if filters else ""
        group = f"GROUP BY {group_mention.column}" if group_mention else ""

        limit_value = int(intent.entities.get("limit", 0)) if intent and intent.entities.get("limit") else 0
        order = ""
        limit = ""
        if group_mention and intent and intent.entities.get("order"):
            order = f"ORDER BY 2 {intent.entities['order']}"
            if limit_value:
                limit = f"LIMIT {limit_value}"

        sql = template.format(where=where, group=group, having="", order=order, limit=limit)
        if group_mention is not None:
            alias = GROUP_ALIAS.get(group_mention.column, group_mention.column)
            # 有些模板（如 publisher_book_cnt）已经把分组列写进 SELECT 了，
            # 再前置一次会得到 `SELECT publisher AS 出版社, publisher AS 出版社, ...`
            head = sql.lstrip()[:160]
            if f"{group_mention.column} AS" not in head:
                sql = sql.replace("SELECT ", f"SELECT {group_mention.column} AS {alias}, ", 1)

        return SQLDraft(
            sql=sql.strip(),
            rationale=f"按指标「{metric.name}」的口径模板生成",
            used_tables=[table],
            source="template",
        )

    def _entity_sql(
        self, question: str, measure: tuple[str, str], mentions: list[Mention], intent: Intent | None,
    ) -> SQLDraft:
        column, alias = measure
        filters = [
            f"{m.column} = '{_esc(m.value)}'" for m in mentions if m.table == "dim_book"
        ]
        where = f"WHERE {' AND '.join(filters)}" if filters else ""
        limit_value = int(intent.entities.get("limit", 0)) if intent and intent.entities.get("limit") else 10
        direction = "ASC" if intent and intent.entities.get("order") == "ASC" else "DESC"
        # 只输出"书名 + 被问的那个量"。多带作者/分类会让结果集比问题要求的更宽，
        # 对用户是噪声，对评测是列数不等。
        sql = (
            f"SELECT title AS 书名, {column} AS {alias} "
            f"FROM v_book_overview {where} ORDER BY {column} {direction} LIMIT {limit_value}"
        )
        return SQLDraft(
            sql=sql.strip(),
            rationale=f"按书籍粒度取明细，用 {alias} 排序",
            used_tables=["v_book_overview"],
            source="template",
        )

    def _detail_sql(self, question: str, mentions: list[Mention], intent: Intent | None) -> SQLDraft:
        filters = [f"{m.column} = '{_esc(m.value)}'" for m in mentions if m.table == "dim_book"]
        where = f"WHERE {' AND '.join(filters)}" if filters else ""
        limit_value = int(intent.entities.get("limit", 0)) if intent and intent.entities.get("limit") else 20
        sql = (
            "SELECT title AS 书名, author_name AS 作者, category AS 分类, "
            f"rating_avg AS 站内评分, rating_cnt AS 评分人数 FROM v_book_overview {where} "
            f"ORDER BY rating_cnt DESC LIMIT {limit_value}"
        )
        return SQLDraft(
            sql=sql.strip(),
            rationale="没有匹配到指标口径，退化为书籍明细查询",
            used_tables=["v_book_overview"],
            source="template",
        )

    # -- insight / critique --------------------------------------------
    def insight(self, state: TurnState) -> Insight:
        return compute_insight(state.result, state.question)

    def critique(self, state: TurnState) -> Verdict:
        return rule_critique(state)


class LLMInferencer:
    """Model-backed inference with the template baseline as its safety net."""

    name = "llm"

    def __init__(self, provider: LLMProvider, fallback: TemplateInferencer) -> None:
        self.provider = provider
        self.fallback = fallback
        # llm_calls / tokens_* / llm_ms 是成本维度的原料：只看准确率不看成本，
        # 会得出「多加一个 Agent 总没错」这种结论，而它通常是错的。
        self.stats = {
            "intent_fallback": 0,
            "sql_fallback": 0,
            "insight_fallback": 0,
            "llm_calls": 0,
            "tokens_in": 0,
            "tokens_out": 0,
            "llm_ms": 0,
        }

    def _record(self, result: Any) -> None:
        """每次真实调用后累计 token 与耗时；回退到模板的调用不计入。"""
        self.stats["llm_calls"] += 1
        self.stats["tokens_in"] += int(getattr(result, "tokens_in", 0) or 0)
        self.stats["tokens_out"] += int(getattr(result, "tokens_out", 0) or 0)
        self.stats["llm_ms"] += int(getattr(result, "ms", 0) or 0)

    # -- intent ---------------------------------------------------------
    def intent(self, state: TurnState) -> Intent:
        try:
            result = self.provider.complete(
                system=prompts.SYSTEM_INTENT,
                user=prompts.build_intent_prompt(state, history=state.history),
                json_mode=True,
            )
            self._record(result)
            data = extract_json(result.text)
            return self._coerce_intent(data, state)
        except Exception as exc:
            logger.warning("intent fallback: %s", exc)
            self.stats["intent_fallback"] += 1
            intent = self.fallback.intent(state)
            intent.defaults_applied.append("模型意图解析失败，已降级为规则解析")
            return intent

    def _coerce_intent(self, data: dict, state: TurnState) -> Intent:
        kind = str(data.get("kind") or "query").strip()
        if kind not in ("query", "chat", "out_of_scope"):
            kind = "query"
        ambiguities: list[Ambiguity] = []
        for raw in data.get("ambiguities") or []:
            if not isinstance(raw, dict):
                continue
            ambiguities.append(
                Ambiguity(
                    term=str(raw.get("term", ""))[:40],
                    options=[str(o)[:40] for o in (raw.get("options") or [])][:4],
                    chosen=str(raw.get("chosen", ""))[:80],
                    reason=str(raw.get("reason", ""))[:200],
                )
            )
        entities = data.get("entities")
        return Intent(
            kind=kind,
            rewritten=str(data.get("rewritten") or state.question).strip(),
            entities=entities if isinstance(entities, dict) else {},
            ambiguities=ambiguities,
            defaults_applied=[str(d)[:120] for d in (data.get("defaults_applied") or [])][:6],
            needs_clarify=bool(data.get("needs_clarify")),
            clarify_question=str(data.get("clarify_question") or "")[:200],
        )

    # -- sql ------------------------------------------------------------
    def sql(self, state: TurnState, feedback: str = "") -> SQLDraft:
        try:
            result = self.provider.complete(
                system=prompts.SYSTEM_SQL,
                user=prompts.build_sql_prompt(state, feedback=feedback),
                json_mode=True,
            )
            self._record(result)
            data = extract_json(result.text)
            sql = str(data.get("sql") or "").strip().rstrip(";")
            if not sql:
                raise LLMError("模型没有给出 SQL")
            used = data.get("used_tables")
            return SQLDraft(
                sql=sql,
                rationale=str(data.get("rationale") or "")[:300],
                used_tables=[str(t) for t in used][:6] if isinstance(used, list) else [],
                source="llm",
            )
        except Exception as exc:
            logger.warning("sql fallback: %s", exc)
            self.stats["sql_fallback"] += 1
            draft = self.fallback.sql(state, feedback)
            draft.source = "fallback"
            draft.rationale = f"{draft.rationale}（模型调用失败：{str(exc)[:80]}）"
            return draft

    # -- insight --------------------------------------------------------
    def insight(self, state: TurnState) -> Insight:
        base = compute_insight(state.result, state.question)
        if state.result is None or state.result.rowcount == 0:
            return base
        try:
            result = self.provider.complete(
                system=prompts.SYSTEM_INSIGHT,
                user=prompts.build_insight_prompt(state, render_table(state.result, 12)),
                json_mode=True,
            )
            self._record(result)
            data = extract_json(result.text)
            headline = str(data.get("headline") or "").strip()
            bullets = [str(b).strip() for b in (data.get("bullets") or []) if str(b).strip()][:3]
            allowed = allowed_numbers(state.result)
            invented = unsupported_numbers(
                " ".join([headline, *bullets]), allowed, state.question
            )
            if invented:
                raise LLMError(f"结论含无法支撑的数字：{invented[:3]}")
            if not headline:
                raise LLMError("结论为空")
            return base.model_copy(update={"headline": headline, "bullets": bullets or base.bullets})
        except Exception as exc:
            logger.warning("insight fallback: %s", exc)
            self.stats["insight_fallback"] += 1
            footnotes = list(base.footnotes) + ["本段结论由规则计算生成（模型表述未通过数字校验）"]
            return base.model_copy(update={"footnotes": footnotes})

    # -- critique -------------------------------------------------------
    def critique(self, state: TurnState) -> Verdict:
        return rule_critique(state)


def build_inferencer(
    dims: DimIndex, provider: LLMProvider | None = None,
) -> TemplateInferencer | LLMInferencer:
    fallback = TemplateInferencer(dims)
    if provider is None:
        return fallback
    return LLMInferencer(provider, fallback)
