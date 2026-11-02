"""语义层三路召回 + 预算裁剪。

三路各自建索引（表 / 指标 / 范例），各自用一份 BM25，因为三路的"相关"定义
不一样：表卡片要看列名和列含义，指标要看别名，范例只看问法。
混在一个索引里，`评分` 这个词会把 `fact_rating`（列里有 score）和
`评分人数`（指标名）压到同一个维度上比较，排序反而不可解释。

四条在契约之外但必要的实现细节，都写在对应函数 docstring 里：
1. `_expand` 做同义词扩展（契约 2.6-3）；
2. `_structure_boost` 做结构加权（契约 2.6-4）；
3. `_two_hop` 做 JOIN 二跳（契约 2.6-5）；
4. `_anchor_metrics` 把命中指标的 `tables` 补进表结果 —— 中文 2-gram 边界
   敏感（`多少本书` 切不出 `书籍`），指标命中了但表没命中的话，下游拿不到
   任何 schema，SQL 一定编不出来。这一条是契约里没有的兜底。
"""
from __future__ import annotations

import logging

from app.core.models import FewShot, MetricCard, SQLContext, TableCard
from app.semantic.bm25 import BM25, tokenize
from app.semantic.loader import SemanticBundle, join_tables

logger = logging.getLogger(__name__)

JOIN_SCORE = 0.01          # 二跳带入的表：只保证"可见"，不参与排序竞争
ANCHOR_SCORE = 0.02        # 指标锚定带入的表
FLOOR_SCORE = 0.05         # 结构命中但 BM25 为 0 时的保底分
NAME_BOOST = 1.5
SYNONYM_BOOST = 1.2


def est_tokens(text: str) -> int:
    """粗略 token 估算：中文一个汉字约 0.6 token，加 1 兜住空串。

    不接 tiktoken 是因为语义层上下文只有几千字，误差不影响决策，
    而多一个依赖就多一份"离线跑不起来"的风险。
    """
    return int(len(text or "") * 0.6) + 1


def table_text(card: TableCard) -> str:
    """表卡片的索引文本 / 成本文本用同一份，避免"索引里算了、预算里没算"。"""
    return " ".join(part for part in (
        card.name, card.meaning, card.grain, " ".join(card.columns),
        " ".join(card.caveats),
    ) if part)


def metric_text(card: MetricCard) -> str:
    return " ".join(part for part in (
        card.name, " ".join(card.aliases), card.definition,
    ) if part)


def example_text(card: FewShot) -> str:
    return " ".join(part for part in (card.question, card.sql, card.note) if part)


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item and item not in seen:
            seen.add(item)
            out.append(item)
    return out


class SemanticRetriever:
    """实现 `core.protocols.Retriever`。"""

    def __init__(self, bundle: SemanticBundle) -> None:
        self._bundle = bundle
        self._tables: list[TableCard] = list(bundle.tables)
        self._metrics: list[MetricCard] = list(bundle.metrics)
        self._examples: list[FewShot] = list(bundle.fewshots)
        self._synonyms: dict[str, list[str]] = dict(bundle.synonyms or {})
        self._join_hints: list[str] = list(bundle.join_hints or [])

        self._table_index: dict[str, TableCard] = {t.name: t for t in self._tables}
        self._metric_index: dict[str, MetricCard] = {m.key: m for m in self._metrics}

        self._table_bm25 = BM25([tokenize(table_text(t)) for t in self._tables])
        self._metric_bm25 = BM25([tokenize(metric_text(m)) for m in self._metrics])
        self._example_bm25 = BM25([tokenize(example_text(e)) for e in self._examples])

        self._neighbors = self._build_join_graph(self._join_hints)

    # ------------------------------------------------------------------
    # 只读查询
    # ------------------------------------------------------------------
    def all_tables(self) -> list[TableCard]:
        return [t.model_copy() for t in self._tables]

    def metric(self, key: str) -> MetricCard | None:
        card = self._metric_index.get(key or "")
        return card.model_copy() if card else None

    def metrics(self) -> list[MetricCard]:
        return [m.model_copy() for m in self._metrics]

    # ------------------------------------------------------------------
    # 主流程
    # ------------------------------------------------------------------
    def search(self, question: str, *, budget: int,
               hints: list[str] | None = None) -> SQLContext:
        """返回一份"能被直接塞进 prompt"的上下文。

        `hints` 是上游 Intent 抽出的实体（`["分类", "2026-10"]`）。它们拼在问题
        后面一起进 BM25，而不是单独建索引：意图里的词只应该"加权"这次检索，
        不应该被当成一个新文档。
        """
        text = " ".join([question or ""] + [str(h) for h in (hints or [])]).strip()
        qtext = text.lower()
        tokens = _dedupe(tokenize(text))
        tokens, syn_hits = self._expand(tokens, qtext)

        tables = self._rank_tables(tokens, qtext, syn_hits)
        metrics = self._rank_metrics(tokens, qtext, syn_hits)
        examples = self._rank_examples(tokens)

        tables = self._two_hop(tables)
        tables = self._anchor_metrics(tables, metrics)

        kept_tables, kept_metrics, kept_examples, dropped, used = self._trim(
            tables, metrics, examples, max(int(budget), 0)
        )
        join_hints = self._pick_join_hints(kept_tables)
        used += est_tokens(" ".join(join_hints)) if join_hints else 0

        logger.debug(
            "search budget=%s q=%r tokens=%d -> tables=%d metrics=%d examples=%d dropped=%d",
            budget, question, len(tokens), len(kept_tables), len(kept_metrics),
            len(kept_examples), dropped,
        )
        return SQLContext(
            tables=kept_tables,
            metrics=kept_metrics,
            examples=kept_examples,
            join_hints=join_hints,
            token_est=used,
            dropped=dropped,
            # 输出约定随上下文一起下发：同一个语义层目录，检索与约定不该是两套来源。
            defaults=self._bundle.defaults,
        )

    # ------------------------------------------------------------------
    # 同义词扩展
    # ------------------------------------------------------------------
    def _expand(self, tokens: list[str], qtext: str) -> tuple[list[str], set[str]]:
        """命中 `synonyms` 的 key 就把 value 的词并进 query。

        为什么必须有这一步：2-gram 对词边界敏感，`多少本书` 切出 `本书`，
        而表卡片里写的是 `书籍`，两者零交集。数据层维护的同义词表是
        唯一能廉价补上这道缝的东西。
        """
        extra: list[str] = []
        hit_terms: set[str] = set()
        for key, values in self._synonyms.items():
            if not key or key.lower() not in qtext:
                continue
            for value in values:
                if not value or value.lower() in qtext:
                    continue                    # 问题里已经写了的不要重复加权
                extra.extend(tokenize(value))
                hit_terms.add(value)
        return _dedupe(tokens + extra), hit_terms

    # ------------------------------------------------------------------
    # 三路排序
    # ------------------------------------------------------------------
    def _structure_boost(self, name: str, aliases: list[str], body: str,
                         qtext: str, syn_hits: set[str]) -> tuple[float, str]:
        """结构加权：名字原文 > 别名 > 同义词。

        返回 `(倍数, hit_reason)`；reason 为空表示没有任何结构命中。
        """
        if name and name.lower() in qtext:
            return NAME_BOOST, "name_exact"
        for alias in aliases:
            if alias and alias.lower() in qtext:
                return SYNONYM_BOOST, f"alias:{alias}"
        for term in syn_hits:
            if term and term.lower() in body:
                return SYNONYM_BOOST, f"synonym:{term}"
        return 1.0, ""

    def _apply(self, base: float, mult: float, reason: str) -> tuple[float, str]:
        score = base * mult
        if mult > 1.0 and score <= 0.0:
            # 结构命中了但没有任何词面交集（典型：只有同义词命中，或超短名）。
            # 给个保底分让它进候选，否则"名字都写在问题里了却召回不到"最难解释。
            score = FLOOR_SCORE
        if reason and score > 0:
            return score, reason
        return score, f"bm25:{base:.2f}"

    def _rank_tables(self, tokens: list[str], qtext: str,
                     syn_hits: set[str]) -> list[TableCard]:
        scores = self._table_bm25.score(tokens)
        ranked: list[TableCard] = []
        for card, base in zip(self._tables, scores):
            mult, reason = self._structure_boost(card.name, [], table_text(card).lower(),
                                                qtext, syn_hits)
            score, hit = self._apply(base, mult, reason)
            if score > 0:
                ranked.append(card.model_copy(update={"score": round(score, 4),
                                                      "hit_reason": hit}))
        ranked.sort(key=lambda c: -c.score)
        return ranked

    def _rank_metrics(self, tokens: list[str], qtext: str,
                      syn_hits: set[str]) -> list[MetricCard]:
        scores = self._metric_bm25.score(tokens)
        ranked: list[MetricCard] = []
        for card, base in zip(self._metrics, scores):
            mult, reason = self._structure_boost(card.name, card.aliases,
                                                metric_text(card).lower(), qtext, syn_hits)
            score, hit = self._apply(base, mult, reason)
            if score > 0:
                ranked.append(card.model_copy(update={"score": round(score, 4),
                                                      "hit_reason": hit}))
        ranked.sort(key=lambda c: -c.score)
        return ranked

    def _rank_examples(self, tokens: list[str]) -> list[FewShot]:
        scores = self._example_bm25.score(tokens)
        ranked = [card.model_copy(update={"score": round(base, 4)})
                  for card, base in zip(self._examples, scores) if base > 0]
        ranked.sort(key=lambda c: -c.score)
        return ranked

    # ------------------------------------------------------------------
    # JOIN 二跳
    # ------------------------------------------------------------------
    @staticmethod
    def _build_join_graph(hints: list[str]) -> dict[str, set[str]]:
        graph: dict[str, set[str]] = {}
        for expr in hints:
            names = join_tables(expr)
            for src in names:
                graph.setdefault(src, set()).update(names - {src})
        return graph

    def _two_hop(self, tables: list[TableCard]) -> list[TableCard]:
        """Top1 表的所有 JOIN 对手表强制补进来。

        单表问题用不到，但只要问题跨表（"评分最高的书"），只召回 dim_book
        就会让 SQLCraft 凭空编一个不存在的 JOIN 条件。补进来的卡片分数记 0.01：
        足以进上下文，又不会把真正的相关表挤下去。
        """
        if not tables:
            return tables
        anchor = tables[0]
        present = {t.name for t in tables}
        for name in sorted(self._neighbors.get(anchor.name, set())):
            card = self._table_index.get(name)
            if card is None or name in present:
                continue
            present.add(name)
            tables.append(card.model_copy(update={"score": JOIN_SCORE,
                                                  "hit_reason": f"join:{anchor.name}"}))
        return tables

    def _anchor_metrics(self, tables: list[TableCard],
                        metrics: list[MetricCard]) -> list[TableCard]:
        """命中指标用的表也必须在上下文里，否则模型不知道 metric 落在哪张表。"""
        present = {t.name for t in tables}
        for metric in metrics:
            for name in metric.tables:
                card = self._table_index.get(name)
                if card is None or name in present:
                    continue
                present.add(name)
                tables.append(card.model_copy(update={
                    "score": ANCHOR_SCORE,
                    "hit_reason": f"metric:{metric.key}",
                }))
        tables.sort(key=lambda c: -c.score)
        return tables

    # ------------------------------------------------------------------
    # 预算裁剪
    # ------------------------------------------------------------------
    def _trim(self, tables: list[TableCard], metrics: list[MetricCard],
              examples: list[FewShot], budget: int
              ) -> tuple[list[TableCard], list[MetricCard], list[FewShot], int, int]:
        """按"表 > 指标 > 范例"的优先级装填，装不下的丢低分项并计数。

        两个硬保底：
        - Top1 表无条件保留（即使超预算）。只剩 40 token 预算时，返回一张
          别的表比返回"主表但略超"更糟 —— 没有表就没有 schema，SQL 必然编错。
        - 范例至少 2 条：没有范例，模型会自由发挥 SQL 风格。
        保底会吃掉预算，此时后续条目自然装不下，`dropped` 照样如实计数。
        """
        used = 0
        dropped = 0

        kept_tables: list[TableCard] = []
        rest = tables
        if tables:
            kept_tables = [tables[0]]
            used += est_tokens(table_text(tables[0]))
            rest = tables[1:]

        for card in rest:
            cost = est_tokens(table_text(card))
            if used + cost <= budget:
                kept_tables.append(card)
                used += cost
            else:
                dropped += 1

        kept_metrics: list[MetricCard] = []
        for card in metrics:
            cost = est_tokens(metric_text(card))
            if used + cost <= budget:
                kept_metrics.append(card)
                used += cost
            else:
                dropped += 1

        kept_examples: list[FewShot] = []
        for card in examples:
            cost = est_tokens(example_text(card))
            if len(kept_examples) < 2 or used + cost <= budget:
                kept_examples.append(card)
                used += cost
            else:
                dropped += 1

        return kept_tables, kept_metrics, kept_examples, dropped, used

    def _pick_join_hints(self, tables: list[TableCard], limit: int = 8) -> list[str]:
        """只留两端都在上下文里的 JOIN，避免提示模型去 JOIN 一张拿不到的表。

        `a.x = b.x` 与 `b.x = a.x` 是同一个连接条件（tables.yml 和 synonyms.yml
        各写一遍很常见），按表对去重，否则 prompt 里会出现重复行。
        """
        selected = {t.name for t in tables}
        exact: list[str] = []
        partial: list[str] = []
        seen: set[frozenset[str]] = set()
        for expr in self._join_hints:
            names = join_tables(expr)
            if not names:
                continue
            pair = frozenset(names)
            if pair in seen:
                continue
            if names <= selected:
                exact.append(expr)
            elif names & selected:
                partial.append(expr)
            else:
                continue
            seen.add(pair)
        return (exact or partial)[:limit]
