"""CI 门禁：把「这次改完有没有退步」变成一个退出码。

规则只有一条主线：**可答率比 baseline 掉超过 `--max-drop`（默认 2pp）就 exit 1**。
另有两条护栏，防止"门禁绿了但数字是假的"：

1. 两个 run 的**套件必须相同**。把 InsightPilot 的 EX 和 DocuMind 的关键词命中
   放一起比，差值毫无意义。
2. 新 run 的**有效题数不能比 baseline 少太多**（默认 80%）。请求失败不进分母，
   所以一串 429 / 超时会让分母从 20 缩到 12，可答率反而可能更好看——那是
   "少考了几道题"，不是"答得更准"。

为什么阈值是 2pp 而不是别的：同一个配置复跑 124 题的波动是 0~2 题（≈0~1.6pp）。
但**20 题的 CI run 一题就是 5pp**，所以 2pp 在 CI 里等价于"一题都不许退步"。
这不是 bug，是有意的：宁可让人看一眼模型变化，也不要让门禁变成一个永远绿的装饰。
真出现连续误报，把 `--max-drop` 调到 5，别去改 baseline。
"""

from __future__ import annotations

from . import compare
from .schema import Run

__all__ = ["check", "format_report"]

UNIT = {"answerable_rate": "pct", "refuse_accuracy": "pct", "cover_rate": "pct",
        "latency_p50_ms": "ms", "cost_per_q_cny": "cny"}


def check(
    baseline: Run,
    new: Run,
    *,
    max_drop_pp: float | None = None,
    max_drop_n: int | None = None,
    min_valid_ratio: float = 0.8,
) -> tuple[bool, str]:
    """返回 ``(是否通过, 人读报告)``。不打印、不退出，方便单测直接调。

    两种阈值互斥，由调用方二选一：

    * ``max_drop_n``：**掉满几题**就红。20 题的 CI run 应该用这个——
      16 道可答题里掉 1 题就是 6.25pp，而"2pp"只有 0.32 题，pp 在这个样本量上
      没有意义（它其实是"一题都不许退步"的另一种写法，还容易让人以为自己
      设了 2 个百分点的宽容度）。`--max-drop-n 2` = 掉 1 题放过、掉 2 题红。
    * ``max_drop_pp``：掉几个百分点。留给样本量足够（≥100 题）的场合。

    都不传时按 2.0pp 处理，保持旧行为。
    """
    if max_drop_pp is not None and max_drop_n is not None:
        raise ValueError("max_drop_pp 与 max_drop_n 互斥，只能给一个")
    if max_drop_pp is None and max_drop_n is None:
        max_drop_pp = 2.0

    problems: list[str] = []
    mb, mn = baseline.metrics(), new.metrics()

    if baseline.suite != new.suite:
        problems.append(f"套件不同：{baseline.suite} vs {new.suite}，分数不可比")

    rb, rn = mb.get("answerable_rate"), mn.get("answerable_rate")
    nb, nn = mb.get("n_answerable") or 0, mn.get("n_answerable") or 0

    if rb is None:
        problems.append("baseline 没有可答率（可能全是必拒题或全是失败题）")
    if rn is None:
        problems.append("新 run 没有可答率（可能全是必拒题或全是失败题）")

    if rb is not None and rn is not None:
        # 分母护栏放在阈值判断前面：分母缩水时先报"不可比"，否则会先报一个
        # 看起来"没掉"或"掉了"的百分比，把人引到错误的方向。
        if nb and nn < nb * min_valid_ratio:
            problems.append(
                f"有效题数从 {nb} 掉到 {nn}（低于 baseline 的 {min_valid_ratio:.0%}），"
                f"分数不可比 —— 先查 n_error={mn.get('n_error')}"
            )
        if max_drop_n is not None:
            pb = mb.get("n_answerable_pass") or 0
            pn = mn.get("n_answerable_pass") or 0
            drop_n = pb - pn
            # 「掉满 N 题就红」：--max-drop-n 2 的语义是掉 1 题放过、掉 2 题失败。
            if drop_n >= max_drop_n:
                problems.append(
                    f"可答题对 {pb} → {pn}（掉 {drop_n} 题，"
                    f"{rb * 100:.2f}% → {rn * 100:.2f}%），阈值是掉满 {max_drop_n} 题"
                )
        else:
            drop_pp = (rb - rn) * 100
            # 容一个 1e-9：可答率是 4 位小数的量化值，50 题里差 1 题算出来是
            # 2.0000000000000018pp，严格 `> 2` 会把「正好 2pp」判成超限。
            if drop_pp > max_drop_pp + 1e-9:
                problems.append(
                    f"可答率 {rb * 100:.2f}% → {rn * 100:.2f}%（跌 {drop_pp:.2f}pp），"
                    f"超过阈值 {max_drop_pp:.2f}pp"
                )

    return (not problems), format_report(baseline, new, problems,
                                         max_drop_pp, max_drop_n)


def format_report(baseline: Run, new: Run, problems: list[str],
                  max_drop_pp: float | None, max_drop_n: int | None) -> str:
    mb, mn = baseline.metrics(), new.metrics()
    L = [f"门禁对比  {_label(baseline)}", f"      →   {_label(new)}", ""]

    rows = [
        ("可答率", "answerable_rate", "pct"),
        ("拒答准确率", "refuse_accuracy", "pct"),
        ("列覆盖率", "cover_rate", "pct"),
        ("p50 延迟", "latency_p50_ms", "ms"),
        ("单题成本", "cost_per_q_cny", "cny"),
        ("有效题/总数", None, None),
    ]
    for name, key, unit in rows:
        if key is None:
            L.append(f"  {name:12s}{mb.get('n_answerable') or 0:>4d} / {mb.get('n') or 0:<4d}"
                     f" → {mn.get('n_answerable') or 0:>4d} / {mn.get('n') or 0:<4d}"
                     f"   (失败 {mb.get('n_error') or 0} → {mn.get('n_error') or 0})")
            continue
        vb, vn = mb.get(key), mn.get(key)
        if vb is None and vn is None:
            continue
        L.append(f"  {name:12s}{_fmt(vb, unit):>12s} → {_fmt(vn, unit):<12s}{_delta(vb, vn, unit)}")

    # 逐题：baseline 与新 run 按题号对齐，指出哪几题从对变错。门禁红了以后
    # 第一眼要看的就是这几行，光标不用来回滚。
    if baseline.items and new.items:
        d = compare.align(baseline, new)
        L.append("")
        L.append(f"逐题（共同 {d['common']} 题）新错 {len(d['new_bad'])} / 新对 {len(d['new_ok'])}")
        if d["new_bad"]:
            L.append(f"  新错：{' '.join(d['new_bad'][:20])}"
                     + (" …" if len(d["new_bad"]) > 20 else ""))
            for qid in d["new_bad"][:5]:
                ia, ib = d["_ia"][qid], d["_ib"][qid]
                L.append(f"    [{qid}] {ia.question[:52]}")
                L.append(f"       旧: {_cut(ia.answer)}")
                L.append(f"       新: {_cut(ib.answer)}")

    L.append("")
    if problems:
        L.append("❌ 不通过")
        L.extend(f"  - {p}" for p in problems)
        L.append("")
        L.append("怎么处理：先看上面的逐题新错，确认是模型/数据变了还是链路坏了；"
                 "确认是有意变更，再人工更新 eval_runs/baseline.json。")
    else:
        rule = (f"可答数未跌满 {max_drop_n} 题" if max_drop_n is not None
                else f"可答率未跌过 {max_drop_pp:.2f}pp")
        L.append(f"✅ 通过（{rule}，有效题数达标）")
    return "\n".join(L)


def _label(r: Run) -> str:
    bits = [r.suite, r.tag or "", r.created_at.replace("T", " ")[:16]]
    return " ".join(b for b in bits if b)


def _fmt(v, unit) -> str:
    if v is None:
        return "—"
    if unit == "pct":
        return f"{v * 100:.2f}%"
    if unit == "ms":
        return f"{int(v):,} ms"
    if unit == "cny":
        return f"¥{v:.6f}"
    return f"{v}"


def _delta(a, b, unit) -> str:
    if a is None or b is None:
        return ""
    d = b - a
    if unit == "pct":
        s = f"{d * 100:+.2f}pp"
    elif unit == "ms":
        s = f"{int(d):+,} ms"
    elif unit == "cny":
        s = f"{d:+.6f}"
    else:
        s = f"{d:+.4f}"
    if unit in ("ms", "cny"):
        mark = "↓ 更好" if d < 0 else ("↑ 更差" if d > 0 else "")
    else:
        mark = "↑ 更好" if d > 0 else ("↓ 更差" if d < 0 else "")
    return f"  ({s}) {mark}".rstrip()


def _cut(s: str, n: int = 80) -> str:
    s = (s or "").replace("\n", " ")
    return s if len(s) <= n else s[:n] + "…"
