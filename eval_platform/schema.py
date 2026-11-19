"""评测中台 · 数据模型与读取层

一个 run 就是一次评测的完整快照：用了哪套金标、跑了哪些题、每题答得怎么样、
汇总指标是多少。所有上层能力（对比、趋势、门禁）都只依赖这一个结构。

对外只暴露三件事：
    load(path) -> Run          读任何历史格式，统一成 Run
    save(run, path)            落盘
    Run.metrics()              汇总指标

历史格式兼容（重要）：工作区里已有 10 个 run 文件，是早期脚本产出的，
结构分两种，本模块两种都认，不做数据迁移、不改老文件。

    旧格式 A（采集）  {"meta": {...}, "items": [...]}
    旧格式 B（评分）  {"meta": {...}, "basic": {...}, "ragas": {...}}
新格式（本中台）
    {"schema": 1, "suite": ..., "tag": ..., "config": {...}, "items": [...], "metrics": {...}}
"""

from __future__ import annotations

import json
import statistics
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1

# 题型：fact = 可答（答对判据是关键词命中），refuse = 必拒（答对判据是拒答且不编造）
TYPE_ANSWERABLE = "fact"
TYPE_REFUSE = "refuse"


# ------------------------------------------------------------------ 数据模型

@dataclass
class Item:
    """一道题的一次作答记录。"""
    id: str
    type: str
    question: str = ""
    task: str = ""
    answer: str = ""
    refused: bool = False
    contexts: list = field(default_factory=list)
    citations: list = field(default_factory=list)
    elapsed_ms: int | None = None
    wall_ms: int | None = None
    cost_cny: float | None = None
    tokens: int | None = None
    error: str | None = None
    # 判分
    kw_hit: bool | None = None       # 可答题：关键词组是否命中（insightpilot 存严格等价 ex）
    kw_leak: bool | None = None      # 是否说了 must_absent 里禁止的内容
    refuse_ok: bool | None = None    # 必拒题：是否正确拒答
    must: list = field(default_factory=list)   # 金标判据（must_include 组），RAGAS 代理召回要用
    judge: dict | None = None        # 套件私有判据明细（insightpilot: ex/cover/diff/sql）
    ragas: dict | None = None        # 可选：该题的 RAGAS 分（score 子命令写入）

    @property
    def passed(self) -> bool | None:
        """这题算不算答对。两类题判据不同。"""
        if self.error:
            return False
        if self.type == TYPE_REFUSE:
            return self.refuse_ok
        if self.kw_hit is None:
            return None
        return bool(self.kw_hit) and not self.kw_leak


@dataclass
class Run:
    suite: str
    tag: str = ""
    created_at: str = ""
    config: dict = field(default_factory=dict)
    items: list[Item] = field(default_factory=list)
    # 来源文件；只用于展示与追溯
    source: str = ""

    # -------------------------------------------------------------- 汇总
    def metrics(self) -> dict:
        """只算能从 items 算出来的；没有 items 时退回读入时带进来的 metrics。"""
        if not self.items:
            return getattr(self, "_metrics", {})

        # 与原脚本 basic_stats 同口径：请求失败的题不进分母。
        # 否则一次网络抖动会被记成"答错"，让分数看起来是质量下降。
        ok = [i for i in self.items if not i.error]
        # 可答 = 一切不是必拒题的题。DocuMind 只有 fact / refuse 两种，等价于
        # `type == "fact"`；InsightPilot 的题型是 T1..T6，用白名单会把分母清空
        # （可答率变成 None），所以这里按"非必拒"取补集。
        ans = [i for i in ok if i.type != TYPE_REFUSE]
        ref = [i for i in ok if i.type == TYPE_REFUSE]
        n_err = len(self.items) - len(ok)

        ans_pass = sum(1 for i in ans if i.passed)
        ref_pass = sum(1 for i in ref if i.refused and not i.kw_leak)
        # 泄漏率只在「有泄漏这个概念」的题上算：InsightPilot 判的是结果集，
        # kw_leak 是 None，把它按 0 计会凭空造出一个"泄漏率 0%"的指标。
        leakable = [i for i in ok if i.kw_leak is not None]
        leaks = sum(1 for i in leakable if i.kw_leak)

        lat = [i.elapsed_ms for i in self.items if i.elapsed_ms]
        # 没有成本字段时给 None 而不是 0：InsightPilot 接口不返回成本，
        # 记成 ¥0 会让人以为「问数不要钱」，那是另一种意义上的假数字。
        costs = [i.cost_cny for i in self.items if i.cost_cny]
        tok = sum(i.tokens for i in self.items if i.tokens)

        m = {
            "n": len(self.items),
            "n_error": n_err,
            "n_answerable": len(ans),
            "n_answerable_pass": ans_pass,
            # 可答率：简历上的主数字，分母是可答题数，不含必拒题
            "answerable_rate": round(ans_pass / len(ans), 4) if ans else None,
            "n_refuse": len(ref),
            "n_refuse_pass": ref_pass,
            "refuse_accuracy": round(ref_pass / len(ref), 4) if ref else None,
            "leak_rate": round(leaks / len(leakable), 4) if leakable else None,
            "latency_p50_ms": int(statistics.median(lat)) if lat else None,
            "latency_p95_ms": _p95(lat),
            "cost_total_cny": round(sum(costs), 6) if costs else None,
            "cost_per_q_cny": round(sum(costs) / len(self.items), 6) if costs else None,
            "tokens_total": tok or None,
        }
        # 列覆盖只在做结果集判定的套件上出现：cover 与 ex 的差就是「答对但多给了列」
        cov = [(i.judge or {}) for i in ok]
        cov = [c for c in cov if isinstance(c, dict) and "cover" in c]
        if cov:
            m["n_cover"] = sum(1 for c in cov if c["cover"])
            m["cover_rate"] = round(sum(1 for c in cov if c["cover"]) / len(cov), 4)
        # RAGAS 只在部分题上算，单独聚合
        ragas = _mean_ragas([i.ragas for i in self.items if i.ragas])
        if ragas:
            m["ragas"] = ragas
        return m

    def to_dict(self) -> dict:
        return {
            "schema": SCHEMA_VERSION,
            "suite": self.suite,
            "tag": self.tag,
            "created_at": self.created_at,
            "config": self.config,
            "items": [asdict(i) for i in self.items],
            "metrics": self.metrics(),
        }


def _p95(xs: list[int]) -> int | None:
    """与 basic_stats 完全同口径：sorted(lat)[int(len*0.95)-1]，题数 <20 时不给值。

    刻意不用统计学标准分位数——新中台要和历史 run 比，口径必须逐位对齐，
    否则 diff 里会出现"同一批数据 p95 变了"的假变化。
    """
    if len(xs) < 20:
        return None
    return int(sorted(xs)[int(len(xs) * 0.95) - 1])


def _mean_ragas(rs: list[dict]) -> dict:
    """按指标名求均值，跳过 None/NaN。"""
    if not rs:
        return {}
    keys: set[str] = set()
    for r in rs:
        keys |= {k for k, v in r.items() if isinstance(v, (int, float))}
    out = {}
    for k in sorted(keys):
        vals = [r[k] for r in rs
                if isinstance(r.get(k), (int, float)) and r[k] == r[k]]  # NaN != NaN
        if vals:
            out[k] = round(sum(vals) / len(vals), 4)
    out["n_scored"] = len(rs)
    return out


# ------------------------------------------------------------------ 读取

def load(path: str | Path) -> Run:
    """读一个 run 文件，任何历史格式都统一成 Run。"""
    p = Path(path)
    d = json.loads(p.read_text(encoding="utf-8"))

    # 新格式
    if d.get("schema") == SCHEMA_VERSION:
        run = Run(
            suite=d.get("suite", "documind"),
            tag=d.get("tag", ""),
            created_at=d.get("created_at", ""),
            config=d.get("config", {}),
            items=[_mk_item(i) for i in d.get("items", [])],
            source=str(p),
        )
        run._metrics = d.get("metrics", {})
        return run

    meta = d.get("meta", {}) or {}

    # 旧格式 A：有 items
    if "items" in d:
        return Run(
            suite=_guess_suite(d),
            tag="",
            created_at=meta.get("created_at", ""),
            config={k: v for k, v in meta.items() if k != "created_at"},
            items=[_mk_item(i) for i in d["items"]],
            source=str(p),
        )

    # 旧格式 B：只有汇总（basic + ragas），没有逐题
    run = Run(
        suite=_guess_suite(d),
        tag="",
        created_at=meta.get("created_at", ""),
        config={k: v for k, v in meta.items() if k != "created_at"},
        items=[],
        source=str(p),
    )
    run._metrics = _from_basic(d)
    return run


def _mk_item(d: dict) -> Item:
    return Item(
        id=d.get("id", ""),
        type=d.get("type", TYPE_ANSWERABLE),
        question=d.get("question") or d.get("q") or "",
        task=d.get("task", ""),
        answer=d.get("answer", "") or "",
        refused=bool(d.get("refused")),
        contexts=d.get("contexts") or [],
        citations=d.get("citations") or [],
        elapsed_ms=d.get("elapsed_ms"),
        wall_ms=d.get("wall_ms"),
        cost_cny=d.get("cost_cny"),
        tokens=d.get("tokens"),
        error=d.get("error"),
        kw_hit=d.get("kw_hit"),
        kw_leak=d.get("kw_leak"),
        refuse_ok=d.get("refuse_ok"),
        must=list(d.get("must_include") or d.get("must") or []),
        judge=d.get("judge"),
        ragas=d.get("ragas"),
    )


def _from_basic(d: dict) -> dict:
    b = d.get("basic", {}) or {}
    r = d.get("ragas", {}) or {}
    out = dict(b)
    if "keyword_hit" in out:          # 旧字段名 → 新字段名
        out["answerable_rate"] = out["keyword_hit"]
    if r:
        out["ragas"] = {k: v for k, v in r.items()
                        if isinstance(v, (int, float)) and v == v}
        if r.get("n_scored") is not None:
            out["ragas"]["n_scored"] = r["n_scored"]
    return out


def _guess_suite(d: dict) -> str:
    """从文件名或内容猜套件名。目前只有 documind 有完整数据。"""
    meta = d.get("meta", {}) or {}
    mode = str(meta.get("mode", "")).lower()
    if mode in ("mix", "naive", "local", "global", "hybrid"):
        return "documind"
    return "documind"


# ------------------------------------------------------------------ 保存

def save(run: Run, path: str | Path) -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(run.to_dict(), ensure_ascii=False, indent=1), encoding="utf-8")
    return p
