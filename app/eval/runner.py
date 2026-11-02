"""单配置跑评测，并把逐题结果聚合成可写进报告的一组指标。

三条纪律，任何一条破了数字就不可信：

1. **必须关缓存**。四组配置共用同一个 runtime.sqlite，``ask`` 的缓存键里虽然
   带了 variant，但依赖它太脆；这里每题都传 ``use_cache=False``，并在指标里
   记录 ``cache_hits``，一旦出现非 0 就是评测框架本身出了问题。
2. **同一 variant 内复用同一个 deps**。``DimIndex.build`` 要扫 6 个维度的
   distinct 值，每题重建会把一次评测从几十秒拖到几分钟；deps 里的 repo /
   retriever / dims 都是只读的，复用不会串题。
3. **attempts 只数 executor**。``report.path.count("executor")`` 是执行次数，
   >1 才说明重试链路真的被触发；用它算首答正确率，而不是看 SQL 像不像。

指标口径
--------
* ``ex``：结果集等价题数 / 总题数（列名、行序不参与，数值 1e-6 相对容差）；
* ``pass_at_1``：``ex and attempts == 1`` 的题数 / 总题数；
* ``p50_ms`` / ``p95_ms``：端到端耗时（``Answer.elapsed_ms``）的最近秩百分位，
  样本 < 20 时 p95 退化为最大值，并在 ``p95_method`` 里注明；
* 失败分布：``failed`` / ``rejected`` / ``clarify`` 按 ``answer.status`` 归类，
  状态 ok 但结果不等的一律记为 ``wrong``。
"""
from __future__ import annotations

import json
import math
import time
from pathlib import Path
from statistics import mean
from typing import Any, Callable

from app.agents.pipeline import Deps, ask, build_deps
from app.core.config import PROJECT_ROOT, Settings, load_settings
from app.core.models import ResultSet
from app.db.repo import QueryFailed, WarehouseRepo
from app.eval.compare import compare, compare_covering

__all__ = [
    "GOLD_PATH",
    "TYPES",
    "aggregate",
    "load_gold",
    "load_gold_results",
    "run_matrix",
    "run_variant",
]

GOLD_PATH = PROJECT_ROOT / "data" / "gold.jsonl"
TYPES = ("T1", "T2", "T3", "T4", "T5", "T6")
FAILURE_KINDS = ("failed", "rejected", "clarify", "wrong")
SMALL_SAMPLE = 20          # 少于这个题数时 p95 直接取最大值
REQUIRED_FIELDS = ("id", "type", "question", "sql", "note")


# --------------------------------------------------------------------------
# 评测集
# --------------------------------------------------------------------------
def load_gold(path: Path | str = GOLD_PATH) -> list[dict[str, str]]:
    """读 JSONL；结构错误直接抛，不静默跳过——静默跳过等于伪造分母。"""
    gold_path = Path(path)
    items: list[dict[str, str]] = []
    seen: set[str] = set()
    for lineno, raw in enumerate(gold_path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"gold 第 {lineno} 行不是合法 JSON：{exc}") from exc
        missing = [key for key in REQUIRED_FIELDS if not str(item.get(key, "")).strip()]
        if missing:
            raise ValueError(f"gold 第 {lineno} 行缺字段：{missing}")
        if item["id"] in seen:
            raise ValueError(f"gold 题号重复：{item['id']}")
        seen.add(item["id"])
        items.append({key: str(item[key]) for key in REQUIRED_FIELDS})
    if not items:
        raise ValueError(f"gold 为空：{gold_path}")
    return items


def load_gold_results(
    gold: list[dict[str, str]], settings: Settings | None = None
) -> dict[str, ResultSet]:
    """把标准 SQL 执行一次，得到每题的真值结果集。"""
    settings = settings or load_settings()
    repo = WarehouseRepo(settings.db_path, settings.row_limit, settings.query_timeout_ms)
    results: dict[str, ResultSet] = {}
    try:
        for item in gold:
            try:
                results[item["id"]] = repo.run_query(item["sql"])
            except QueryFailed as exc:
                raise ValueError(
                    f"gold {item['id']} 执行失败（{exc.stage}）：{exc.message}"
                ) from exc
    finally:
        repo.close()
    return results


# --------------------------------------------------------------------------
# 单题
# --------------------------------------------------------------------------
def _evaluate_item(
    item: dict[str, str], gold_result: ResultSet, deps: Deps
) -> dict[str, Any]:
    started = time.perf_counter()
    error = ""
    sql = ""
    try:
        answer, report = ask(item["question"], deps=deps, use_cache=False)
        sql = answer.sql
        status = answer.status
        pred = ResultSet(
            columns=answer.columns,
            rows=answer.rows,
            rowcount=answer.rowcount,
        )
        ex, diff = compare(gold_result, pred)
        # 严格判定已通过时，覆盖判定必然通过，不必重复计算。
        if ex:
            cover, cover_diff = True, "结果一致"
        else:
            cover, cover_diff = compare_covering(gold_result, pred)
        attempts = report.path.count("executor")
        cache_hit = bool(answer.cached) or report.path[:1] == ["cache"]
        elapsed_ms = int(answer.elapsed_ms or 0)
        path = list(report.path)
        if status != "ok":
            error = answer.message or status
    except Exception as exc:  # 单题异常不能打断 60 题的整轮评测
        status = "failed"
        ex, diff = False, f"评测异常：{type(exc).__name__}: {exc}"
        cover, cover_diff = False, diff
        attempts = 0
        cache_hit = False
        elapsed_ms = 0
        path = []
        error = diff

    wall_ms = int((time.perf_counter() - started) * 1000)
    if not elapsed_ms:
        elapsed_ms = wall_ms

    return {
        "id": item["id"],
        "type": item["type"],
        "ex": bool(ex),
        "cover": bool(cover),
        "cover_diff": cover_diff,
        "first_try_correct": bool(ex and attempts == 1),
        "attempts": int(attempts),
        "elapsed_ms": int(elapsed_ms),
        "wall_ms": wall_ms,
        "status": status,
        "sql": sql,
        "gold_sql": item["sql"],
        "diff": diff,
        "error": error,
        "cache_hit": bool(cache_hit),
        "path": path,
    }


# --------------------------------------------------------------------------
# 聚合
# --------------------------------------------------------------------------
def _percentile(sorted_values: list[int], q: float) -> int:
    """最近秩法：第 ceil(q/100*n) 个值（1-based），对 1~2 个样本也稳定。"""
    if not sorted_values:
        return 0
    index = max(0, math.ceil(q / 100.0 * len(sorted_values)) - 1)
    return sorted_values[min(index, len(sorted_values) - 1)]


def aggregate(
    items: list[dict[str, Any]],
    *,
    variant: str,
    mode: str,
    inferencer: str,
    limit: int | None = None,
) -> dict[str, Any]:
    total = len(items)
    if total == 0:
        raise ValueError(f"variant {variant!r} 没有可评测的题目")

    ex_count = sum(1 for item in items if item["ex"])
    # 兼容旧结果文件：没有 cover 字段时退化为 ex，不会把历史数据算成 0。
    cover_count = sum(1 for item in items if item.get("cover", item["ex"]))
    first_count = sum(1 for item in items if item["first_try_correct"])
    attempts = [int(item["attempts"]) for item in items]
    elapsed = sorted(int(item["elapsed_ms"]) for item in items)

    type_rows: dict[str, dict[str, Any]] = {}
    for qtype in TYPES:
        subset = [item for item in items if item["type"] == qtype]
        if not subset:
            continue
        type_ex = sum(1 for item in subset if item["ex"])
        type_cover = sum(1 for item in subset if item.get("cover", item["ex"]))
        type_first = sum(1 for item in subset if item["first_try_correct"])
        type_rows[qtype] = {
            "total": len(subset),
            "ex_count": type_ex,
            "ex": round(type_ex / len(subset), 4),
            "cover_count": type_cover,
            "cover": round(type_cover / len(subset), 4),
            "pass_at_1": round(type_first / len(subset), 4),
        }

    failures = {kind: 0 for kind in FAILURE_KINDS}
    for item in items:
        if item["ex"]:
            continue
        status = item["status"] if item["status"] in ("failed", "rejected", "clarify") else "wrong"
        failures[status] += 1

    return {
        "variant": variant,
        "mode": mode,
        "inferencer": inferencer,
        "limit": limit,
        "total": total,
        "ex_count": ex_count,
        "ex": round(ex_count / total, 4),
        "cover_count": cover_count,
        "cover": round(cover_count / total, 4),
        "cover_gap": cover_count - ex_count,
        "pass_at_1_count": first_count,
        "pass_at_1": round(first_count / total, 4),
        "avg_attempts": round(mean(attempts), 3),
        "attempts_gt1": sum(1 for value in attempts if value > 1),
        "p50_ms": _percentile(elapsed, 50),
        "p95_ms": _percentile(elapsed, 95),
        "p95_method": "max (n<20)" if total < SMALL_SAMPLE else "nearest-rank",
        "cache_hits": sum(1 for item in items if item["cache_hit"]),
        "types": type_rows,
        "failures": failures,
    }


# --------------------------------------------------------------------------
# 单配置 / 多配置
# --------------------------------------------------------------------------
def run_variant(
    variant: str,
    gold: list[dict[str, str]],
    *,
    deps: Deps | None = None,
    limit: int | None = None,
    mode: str = "template",
    sleep: float = 0.0,
    gold_results: dict[str, ResultSet] | None = None,
    on_item: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """跑完一个 variant，返回 ``{"metrics": ..., "items": [...]}``。"""
    selected = gold[:limit] if limit else gold
    owns_deps = deps is None
    deps = deps or build_deps(force_template=(mode == "template"), variant=variant)
    if gold_results is None:
        gold_results = load_gold_results(selected, deps.settings)

    items: list[dict[str, Any]] = []
    try:
        for index, item in enumerate(selected):
            row = _evaluate_item(item, gold_results[item["id"]], deps)
            items.append(row)
            if on_item is not None:
                on_item(row)
            if sleep > 0 and index < len(selected) - 1:
                time.sleep(sleep)
    finally:
        if owns_deps:
            deps.repo.close()

    metrics = aggregate(
        items,
        variant=variant,
        mode=deps.settings.effective_mode,
        inferencer=deps.inferencer.name,
        limit=limit,
    )
    metrics.update(_degradation(deps, total=len(selected)))
    return {"metrics": metrics, "items": items}


def _degradation(deps: Deps, *, total: int) -> dict[str, Any]:
    """算出这轮有多少题其实是规则模板答的，以及花了多少 token / 多少钱。

    为什么必须记降级率：``LLMInferencer`` 在模型报错（401 / 超时 / 返回不含 SQL）
    时会静默回退到 ``TemplateInferencer``，链路照样返回 ``status=ok``。如果评测
    只看 EX 不看回退率，一份"LLM 模式"的报告可能 100% 是模板跑出来的——
    那不是模型的效果，是造假的数字。所以这里把回退计数显式抬到指标层面，
    由调用方决定是拒收还是标注。

    为什么同时记成本：只看准确率会得出「多加一个 Agent 总没错」的结论。
    把 token 与耗时并列出来，才能回答「关掉 Critic 省了多少、损失了多少」。
    单价来自 ``Settings.price_*_per_m``，默认按公开价填，只用于横向比较。
    """
    stats = getattr(deps.inferencer, "stats", None)
    if not isinstance(stats, dict) or not total:
        return {
            "llm_calls": None, "fallback": None, "degraded": False, "degraded_reason": "",
            "tokens_in": None, "tokens_out": None, "tokens_per_question": None,
            "cost_cny": None, "cost_per_question_cny": None, "llm_ms_per_question": None,
        }

    counters = {
        key: int(value) for key, value in stats.items()
        if isinstance(value, int)
    }
    sql_fb = counters.get("sql_fallback", 0)
    intent_fb = counters.get("intent_fallback", 0)
    ratio = sql_fb / total

    reasons: list[str] = []
    if ratio >= 0.5:
        reasons.append(f"{sql_fb}/{total} 题的 SQL 来自规则模板回退（{ratio:.0%}）")
    if intent_fb >= total:
        reasons.append(f"全部 {total} 题的意图解析都回退了")

    tokens_in = counters.get("tokens_in", 0)
    tokens_out = counters.get("tokens_out", 0)
    llm_ms = counters.get("llm_ms", 0)

    # 成本是附加信息：拿不到 settings（单测里的假依赖、或换了实现）就返回 None，
    # 不能因为算不出钱把整轮评测的聚合打挂。
    settings = getattr(deps, "settings", None)
    cost_fn = getattr(settings, "cost_cny", None)
    cost = cost_fn(tokens_in, tokens_out) if callable(cost_fn) else None

    return {
        "llm_calls": counters.get("llm_calls", 0),
        "fallback": counters,
        "degraded": bool(reasons),
        "degraded_reason": "；".join(reasons),
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "tokens_per_question": round((tokens_in + tokens_out) / total, 1),
        "cost_cny": round(cost, 4) if cost is not None else None,
        "cost_per_question_cny": round(cost / total, 5) if cost is not None else None,
        "llm_ms_per_question": round(llm_ms / total, 1),
    }


def run_matrix(
    variants: list[str],
    gold: list[dict[str, str]],
    *,
    limit: int | None = None,
    mode: str = "template",
    sleep: float = 0.0,
    on_item: Callable[[str, dict[str, Any]], None] | None = None,
    on_variant: Callable[[str, dict[str, Any]], None] | None = None,
) -> dict[str, dict[str, Any]]:
    """逐个 variant 跑；每个 variant 只建一次 deps，跑完关连接。"""
    selected = gold[:limit] if limit else gold
    gold_results = load_gold_results(selected)
    results: dict[str, dict[str, Any]] = {}

    for variant in variants:
        deps = build_deps(force_template=(mode == "template"), variant=variant)
        try:
            results[variant] = run_variant(
                variant,
                gold,
                deps=deps,
                limit=limit,
                mode=mode,
                sleep=sleep,
                gold_results=gold_results,
                on_item=(lambda row, v=variant: on_item(v, row)) if on_item else None,
            )
        finally:
            deps.repo.close()
        if on_variant is not None:
            on_variant(variant, results[variant]["metrics"])
    return results
