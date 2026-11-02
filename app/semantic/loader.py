"""语义层 YAML 加载器。

对外只暴露 `SemanticBundle` 与 `load_semantic(directory)`。

容错优先级高于严格校验：语义层是四个 Agent 并行写的产物，少一个 `caveats`、
少一个 `synonyms.yml` 都不该让整条链路 500。所以这里对"缺字段"一律补默认值，
只有"缺 name / 缺 key"这种无法定位卡片的情况才跳过该条并记 warning。

一个取舍：`TableCard.columns` 在 core.models 里是 `list[str]`，没有列级
synonyms 字段。为了让"评分"能命中 `rating_avg`，这里把列的意义和别名折叠进
字符串：`rating_avg: 站内平均评分 0-10（别名: 评分/打分）`。既保住了契约签名，
又让列级信息进入 BM25 索引；下游拼 prompt 时列名仍然是行首，不影响取数。
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from app.core.models import FewShot, MetricCard, TableCard

logger = logging.getLogger(__name__)

_TABLE_COL = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)\.([A-Za-z_][A-Za-z0-9_]*)")

# 输出约定的兜底值。defaults.yml 缺失、损坏或漏字段时用这套，
# 保证「没有配置文件」和「配置写了一半」都还能跑出可预期的 SQL。
FALLBACK_DEFAULTS: dict[str, dict[str, Any]] = {
    "limits": {"ranking": 10, "single": 1, "detail": 50},
    "format": {"decimal_places": 2, "percent_scale": 100.0, "integer_places": 0},
    "stats": {"min_group_size": 5},
}

_DEFAULT_SECTIONS = ("limits", "format", "stats")


@dataclass
class SemanticBundle:
    tables: list[TableCard] = field(default_factory=list)
    metrics: list[MetricCard] = field(default_factory=list)
    fewshots: list[FewShot] = field(default_factory=list)
    synonyms: dict[str, list[str]] = field(default_factory=dict)
    join_hints: list[str] = field(default_factory=list)
    defaults: dict[str, Any] = field(default_factory=dict)


# --------------------------------------------------------------------------
# YAML 原语（所有形状都兜住）
# --------------------------------------------------------------------------
def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        logger.warning("semantic file missing: %s", path)
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:            # 坏文件不该炸整条链路
        logger.warning("semantic file unreadable: %s (%s)", path, exc)
        return {}
    return data if isinstance(data, dict) else {}


def _as_str(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    return str(value).strip()


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return list(value)
    if isinstance(value, str):
        return [value] if value.strip() else []
    return [value]


def _str_list(value: Any) -> list[str]:
    """把 `[a, b]`、`a` 两种写法都收敛成 `["a", "b"]`，并丢掉空串和重复。"""
    out: list[str] = []
    for item in _as_list(value):
        text = _as_str(item)
        if text and text not in out:
            out.append(text)
    return out


# --------------------------------------------------------------------------
# 各文件
# --------------------------------------------------------------------------
def _column_text(raw: Any) -> str:
    if isinstance(raw, str):
        return raw.strip()
    if not isinstance(raw, dict):
        return ""
    name = _as_str(raw.get("name"))
    if not name:
        return ""
    text = name
    meaning = _as_str(raw.get("meaning"))
    if meaning:
        text += f": {meaning}"
    synonyms = _str_list(raw.get("synonyms"))
    if synonyms:
        text += f"（别名: {'/'.join(synonyms)}）"
    return text


def _load_tables(doc: dict[str, Any], joins: list[str]) -> list[TableCard]:
    tables: list[TableCard] = []
    for raw in _as_list(doc.get("tables")):
        if not isinstance(raw, dict):
            continue
        name = _as_str(raw.get("name"))
        if not name:
            logger.warning("skip table without name: %r", raw)
            continue
        columns = [c for c in (_column_text(x) for x in _as_list(raw.get("columns"))) if c]
        tables.append(TableCard(
            name=name,
            grain=_as_str(raw.get("grain")),
            columns=columns,
            meaning=_as_str(raw.get("meaning")),
            caveats=_str_list(raw.get("caveats")),
        ))
        for expr in _str_list(raw.get("joins")):
            if expr not in joins:
                joins.append(expr)
    return tables


def _load_metrics(doc: dict[str, Any]) -> list[MetricCard]:
    metrics: list[MetricCard] = []
    for raw in _as_list(doc.get("metrics")):
        if not isinstance(raw, dict):
            continue
        key = _as_str(raw.get("key"))
        if not key:
            logger.warning("skip metric without key: %r", raw)
            continue
        metrics.append(MetricCard(
            key=key,
            name=_as_str(raw.get("name")) or key,
            aliases=_str_list(raw.get("aliases")),
            definition=_as_str(raw.get("definition")),
            formula=_as_str(raw.get("formula")),
            unit=_as_str(raw.get("unit")),
            tables=_str_list(raw.get("tables")),
            caveats=_str_list(raw.get("caveats")),
            sql_template=_as_str(raw.get("sql_template")),
        ))
    return metrics


def _load_fewshots(doc: dict[str, Any]) -> list[FewShot]:
    shots: list[FewShot] = []
    for raw in _as_list(doc.get("examples")) or _as_list(doc.get("fewshots")):
        if not isinstance(raw, dict):
            continue
        question = _as_str(raw.get("question"))
        if not question:
            continue
        shots.append(FewShot(
            question=question,
            sql=_as_str(raw.get("sql")),
            note=_as_str(raw.get("note")),
        ))
    return shots


def _load_synonyms(doc: dict[str, Any], joins: list[str]) -> dict[str, list[str]]:
    raw = doc.get("synonyms")
    synonyms: dict[str, list[str]] = {}
    if isinstance(raw, dict):
        for key, values in raw.items():
            name = _as_str(key)
            if not name:
                continue
            vals = _str_list(values)
            if vals:
                synonyms[name] = vals
    for expr in _str_list(doc.get("joins")):
        if expr not in joins:
            joins.append(expr)
    return synonyms


# --------------------------------------------------------------------------
# 入口
# --------------------------------------------------------------------------
def _merge_defaults(doc: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """把 defaults.yml 叠到兜底值上：只认数值，字段缺失保持兜底。

    不做 `dict.update` 整体替换，否则只写了 `limits` 的配置会把
    `format` / `stats` 整段抹掉，下游读不到小数位。
    """
    merged = {section: dict(values) for section, values in FALLBACK_DEFAULTS.items()}
    for section in _DEFAULT_SECTIONS:
        raw = doc.get(section)
        if not isinstance(raw, dict):
            continue
        for key, value in raw.items():
            # bool 是 int 子类，先排除；字符串等非数值一律忽略，避免把
            # "10 行" 这种写法当成行数塞进 SQL。
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                continue
            merged[section][str(key)] = value
    return merged


def load_semantic(directory: Path) -> SemanticBundle:
    """读一个语义层目录；任何文件缺失/损坏都退化成空集合而不是抛异常。"""
    root = Path(directory)
    joins: list[str] = []

    tables = _load_tables(_read_yaml(root / "tables.yml"), joins)
    metrics = _load_metrics(_read_yaml(root / "metrics.yml"))
    fewshots = _load_fewshots(_read_yaml(root / "fewshots.yml"))
    synonyms = _load_synonyms(_read_yaml(root / "synonyms.yml"), joins)
    defaults = _merge_defaults(_read_yaml(root / "defaults.yml"))

    logger.info(
        "semantic bundle loaded from %s: %d tables, %d metrics, %d examples, "
        "%d synonym keys, %d join hints, defaults=%s",
        root, len(tables), len(metrics), len(fewshots), len(synonyms), len(joins),
        defaults.get("limits", {}),
    )
    return SemanticBundle(
        tables=tables,
        metrics=metrics,
        fewshots=fewshots,
        synonyms=synonyms,
        join_hints=joins,
        defaults=defaults,
    )


def join_tables(expr: str) -> set[str]:
    """从 `a.c = b.c` 里解析出参与的表名，供二跳扩展建邻接表用。"""
    return {m.group(1) for m in _TABLE_COL.finditer(expr or "")}
