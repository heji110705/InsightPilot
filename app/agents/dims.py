"""Dimension-value linking and shallow question parsing.

Before a model writes SQL it helps to know that "人民文学出版社" is a real value
of `dim_book.publisher` and not a table name. Real rows are the cheapest source
of that knowledge, so this module reads the distinct values once and matches
them against the question by longest-substring.

This is also what the no-model baseline leans on: with dimension linking plus a
metric template, ~half of the gold questions can be answered correctly without
any LLM at all, which makes the ablation a fair comparison rather than a straw man.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from app.core.protocols import Repo

logger = logging.getLogger(__name__)

DIMENSION_FIELDS: tuple[tuple[str, str], ...] = (
    ("dim_book", "category"),
    ("dim_book", "publisher"),
    ("dim_book", "author_name"),
    ("dim_book", "binding"),
    ("fact_shelf", "status"),
    ("fact_click", "event"),
)

DATE_COLUMNS = {
    "fact_rating": "rated_on",
    "fact_comment": "created_on",
    "fact_click": "clicked_on",
    "fact_shelf": "created_on",
}

_ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
_YEAR = re.compile(r"(20\d{2})\s*年")
_LIMIT_PATTERNS = (
    re.compile(r"(?:前|top|TOP|Top)\s*(\d{1,3})"),
    re.compile(r"(\d{1,3})\s*(?:本|条|个|位|名|种|类|家|年)"),
)
_CN_DIGITS = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5,
              "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
_CN_LIMIT = re.compile(r"(?:前|top|TOP|Top)\s*([一二三四五六七八九十]{1,2})")
_CN_COUNT = re.compile(r"([一二三四五六七八九十]{1,2})\s*(?:本|条|个|位|名|种|类|家|年)")
_GROUP_HINTS = (
    "各", "按", "每", "分别", "分组", "哪个", "哪家", "哪几", "哪年",
    "各种", "各类", "不同", "分布",
)
_YEAR_GROUP = ("哪年", "哪几年", "年份", "每年", "按年")
_DESC_HINTS = ("最高", "最多", "最大", "最热", "最受欢迎", "最好", "排名", "降序")
_ASC_HINTS = ("最低", "最少", "最小", "最少人", "升序")
_VAGUE_TERMS = {
    "最受欢迎": ["点击量最高", "收藏数最多", "评分人数最多"],
    "最热门": ["点击量最高", "收藏数最多"],
    "最好的书": ["站内平均评分最高", "豆瓣评分最高"],
    "评分最高": ["站内平均评分最高", "豆瓣评分最高"],
}

_NOISE = re.compile(
    r"(请问|帮我|帮忙|查一下|看一下|看看|我想知道|麻烦|一下|呗|呢|啊|吗|嘛|我想|能不能|可不可以)"
)


@dataclass(slots=True)
class Mention:
    table: str
    column: str
    value: str


@dataclass
class DimIndex:
    """Distinct dimension values, loaded once per process."""

    values: dict[str, list[str]] = field(default_factory=dict)

    @classmethod
    def build(cls, repo: Repo, *, max_values: int = 400) -> "DimIndex":
        values: dict[str, list[str]] = {}
        for table, column in DIMENSION_FIELDS:
            key = f"{table}.{column}"
            try:
                result = repo.run_query(
                    f"SELECT DISTINCT {column} AS v FROM {table} "
                    f"WHERE {column} IS NOT NULL AND TRIM({column}) <> '' LIMIT {max_values}"
                )
            except Exception as exc:  # missing table or column must not be fatal
                logger.warning("dimension load skipped for %s: %s", key, exc)
                continue
            items = [str(row[0]).strip() for row in result.rows if row and row[0] is not None]
            # 短值（"其他"、"精装"）噪声大，长值（出版社全名）信息量高，都保留但排序匹配时优先长的
            values[key] = sorted({v for v in items if len(v) >= 2}, key=len, reverse=True)
        return cls(values=values)

    # ------------------------------------------------------------------
    def mentions(self, question: str) -> list[Mention]:
        """Longest-match-first so "人民文学出版社" wins over "人民文学"."""
        found: list[Mention] = []
        claimed: list[str] = []
        for key in ("dim_book.category", "dim_book.publisher", "dim_book.author_name",
                    "fact_shelf.status", "fact_click.event", "dim_book.binding"):
            table, column = key.split(".")
            for value in self.values.get(key, []):
                if value not in question:
                    continue
                if any(value in other and value != other for other in claimed):
                    continue
                if any(m.table == table and m.column == column and m.value == value for m in found):
                    continue
                found.append(Mention(table=table, column=column, value=value))
                claimed.append(value)
                break  # one value per column keeps the WHERE clause sane
        return found


def clean_question(question: str) -> str:
    text = _NOISE.sub("", question or "")
    text = re.sub(r"\s+", " ", text).strip(" ，,。?？!！")
    return text or (question or "").strip()


def _cn_to_int(token: str) -> int | None:
    """Convert 一…十 (and 十X / X十) to an int; None when it is not a numeral."""
    if token in _CN_DIGITS:
        return _CN_DIGITS[token]
    if len(token) == 2:
        head, tail = token[0], token[1]
        if head == "十" and tail in _CN_DIGITS:
            return 10 + _CN_DIGITS[tail]
        if tail == "十" and head in _CN_DIGITS:
            return _CN_DIGITS[head] * 10
    return None


def detect_limit(question: str) -> int | None:
    text = question or ""
    for pattern in _LIMIT_PATTERNS:
        match = pattern.search(text)
        if match:
            value = int(match.group(1))
            if 1 <= value <= 200:
                return value
    for pattern in (_CN_LIMIT, _CN_COUNT):
        match = pattern.search(text)
        if match:
            value = _cn_to_int(match.group(1))
            if value and 1 <= value <= 200:
                return value
    return None


def wants_year_group(question: str) -> bool:
    return any(hint in (question or "") for hint in _YEAR_GROUP)


def detect_group(question: str) -> bool:
    return any(hint in (question or "") for hint in _GROUP_HINTS)


def detect_order(question: str) -> str | None:
    if any(hint in (question or "") for hint in _ASC_HINTS):
        return "ASC"
    if any(hint in (question or "") for hint in _DESC_HINTS):
        return "DESC"
    return None


def detect_vague(question: str) -> list[tuple[str, list[str]]]:
    hits: list[tuple[str, list[str]]] = []
    for term, options in _VAGUE_TERMS.items():
        if term in (question or ""):
            hits.append((term, list(options)))
    return hits


def detect_kind(question: str) -> str:
    """query / chat / out_of_scope — deliberately crude in template mode."""
    text = (question or "").strip()
    if len(text) <= 3:
        return "chat"
    data_words = (
        "书", "评分", "评论", "作者", "出版", "点击", "收藏", "书架", "分类",
        "用户", "数据", "多少", "几本", "哪些", "排名", "占比", "平均", "统计",
        "趋势", "变化", "总数", "分布", "最高", "最低", "最多", "最少",
    )
    if any(word in text for word in data_words):
        return "query"
    if re.search(r"(你好|您好|hi|hello|谢谢|在吗)", text, re.IGNORECASE):
        return "chat"
    return "out_of_scope"


def time_filter(question: str, column: str | None) -> str:
    """Only emit a predicate when the question names a concrete date or year."""
    if not column:
        return ""
    dates = sorted(set(_ISO_DATE.findall(question or "")))
    if len(dates) >= 2:
        return f"{column} BETWEEN '{dates[0]}' AND '{dates[-1]}'"
    if len(dates) == 1:
        return f"{column} = '{dates[0]}'"
    year = _YEAR.search(question or "")
    if year:
        return f"substr({column}, 1, 4) = '{year.group(1)}'"
    return ""


def primary_table(sql_or_tables: str | list[str]) -> str | None:
    tables = [sql_or_tables] if isinstance(sql_or_tables, str) else list(sql_or_tables)
    for candidate in ("fact_rating", "fact_comment", "fact_click", "fact_shelf",
                      "v_book_overview", "dim_book", "dim_author", "dim_date"):
        for text in tables:
            if candidate in text:
                return candidate
    return None
