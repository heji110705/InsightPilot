"""把多次运行的评测结果汇总成「均值 ± 波动」，用来判断消融差异是否可信。

为什么需要它：temperature=0 不等于确定性（MoE 路由、批处理、服务端采样都会带噪声）。
本项目的实测是同一配置连跑两次 EX 差 3.3 个百分点（41.7% vs 45.0%），而四组消融
之间的差距也是这个量级。**不做重复性检验就宣布"某模块带来提升"，是在噪声上写结论。**

用法：
    D:/Python312/python.exe scripts/repeatability.py --runs docs _work/repeat _work/repeat2 \\
        --out docs/03b-重复性检验.md
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from statistics import mean

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

RESULTS_NAME = "eval_results.json"
VARIANT_LABEL = {
    "full": "full（6 Agent 全开）",
    "no_critic": "no_critic（去掉自检）",
    "no_schema": "no_schema（去掉语义层检索）",
    "single": "single（单 Agent，无检索/重试/自检）",
}
ORDER = ("full", "no_critic", "no_schema", "single")


def _load(run_dir: Path) -> dict:
    path = run_dir / RESULTS_NAME
    if not path.exists():
        raise SystemExit(f"[FATAL] {path} 不存在")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not data:
        raise SystemExit(f"[FATAL] {path} 为空")
    return data


def _collect(dirs: list[Path]) -> dict[str, list[dict]]:
    """variant -> [每次运行的 metrics, ...]，按运行顺序排列。"""
    out: dict[str, list[dict]] = {}
    for run_dir in dirs:
        for variant, entry in _load(run_dir).items():
            out.setdefault(variant, []).append(entry["metrics"])
    return out


def _pct(value: float) -> str:
    return f"{value * 100:.1f}%"


def _stats(values: list[float]) -> tuple[float, float, float, float]:
    return mean(values), min(values), max(values), (max(values) - min(values)) * 100


def build_report(collected: dict[str, list[dict]], run_dirs: list[Path]) -> str:
    variants = [v for v in ORDER if v in collected] + [
        v for v in collected if v not in ORDER
    ]
    modes = {m.get("mode") for runs in collected.values() for m in runs}
    lines = [
        "# 重复性检验与消融可信度",
        "",
        f"> 数据来源：{len(run_dirs)} 个运行目录，共 "
        f"{sum(len(r) for r in collected.values())} 次配置运行；模式 "
        f"{'/'.join(sorted(x for x in modes if x))}。全部数字由本脚本从各目录的 "
        "`eval_results.json` 汇总，无手填。",
        "",
        "## 1. 结论",
        "",
    ]

    spreads: dict[str, float] = {}
    for variant in variants:
        values = [m["ex"] for m in collected[variant]]
        spreads[variant] = (max(values) - min(values)) * 100
    noisiest = max(spreads, key=lambda v: spreads[v]) if spreads else variants[0]
    noise = spreads.get(noisiest, 0.0)
    noisy_values = [m["ex"] for m in collected[noisiest]]
    best = max(variants, key=lambda v: mean(m["ex"] for m in collected[v]))
    worst = min(variants, key=lambda v: mean(m["ex"] for m in collected[v]))
    best_mean = mean(m["ex"] for m in collected[best])
    worst_mean = mean(m["ex"] for m in collected[worst])

    lines += [
        f"同一配置重复运行的 EX 极差最大为 **{noise:.1f} 个百分点**"
        f"（{_pct(min(noisy_values))} ~ {_pct(max(noisy_values))}，配置 `{noisiest}`）。",
        "",
        f"四组配置的平均 EX 落在 {_pct(worst_mean)} ~ {_pct(best_mean)} 之间，"
        f"极差 {abs(best_mean - worst_mean) * 100:.1f} 个百分点。",
        "",
    ]

    min_n = min(len(collected[v]) for v in variants)
    if min_n < 2:
        lines += [
            f"**样本不足**：至少一个配置只有 {min_n} 次运行，无法区分"
            "「配置差异」和「运行噪声」。本表只作记录，不据此下任何消融结论；"
            "请补齐每个配置的重复次数后重跑本脚本。",
            "",
        ]
    elif abs(best_mean - worst_mean) * 100 <= noise:
        lines += [
            "**因此本实验不支持「某个模块带来准确率提升」这类结论**：组间差异小于"
            "同配置重复运行的波动，属于噪声。任何引用本表的说法都应停留在"
            "「四个变体在 60 题上表现接近」，不能写成「多 Agent 提升了 X%」。",
            "",
        ]
    else:
        lines += [
            "组间极差大于重复波动，差异**具备进一步验证的价值**，但 n 仍然很小"
            f"（每配置 {min_n} 次），不能作为显著性结论；如需下判断应扩到每配置 ≥5 次。",
            "",
        ]

    lines += [
        "## 2. 逐配置明细",
        "",
        "| 配置 | 运行次数 | EX 各次 | 均值 | 极差 | 首答正确均值 | p50 均值 |",
        "|---|---|---|---|---|---|---|",
    ]
    for variant in variants:
        runs = collected[variant]
        values = [m["ex"] for m in runs]
        avg, low, high, spread = _stats(values)
        each = " / ".join(_pct(v) for v in values)
        lines.append(
            f"| {VARIANT_LABEL.get(variant, variant)} | {len(runs)} | {each} "
            f"| {_pct(avg)} | {spread:.1f}pp "
            f"| {_pct(mean(m['pass_at_1'] for m in runs))} "
            f"| {int(mean(m['p50_ms'] for m in runs))} ms |"
        )
    lines += [
        "",
        f"> 极差单位 pp = 百分点。波动带 **±{noise / 2:.1f}pp**"
        "（取各配置极差的半宽，样本量小时这是粗略上界而非置信区间）。",
        "",
        "## 3. 分类型 EX（各次均值）",
        "",
    ]

    types = sorted({t for runs in collected.values() for m in runs for t in m.get("types", {})})
    header = "| 配置 | " + " | ".join(types) + " |"
    lines += [header, "|---|" + "---|" * len(types)]
    for variant in variants:
        runs = collected[variant]
        cells: list[str] = []
        for qtype in types:
            values = [
                m["types"][qtype]["ex"] for m in runs if qtype in m.get("types", {})
            ]
            cells.append(_pct(mean(values)) if values else "—")
        lines.append(f"| {VARIANT_LABEL.get(variant, variant)} | " + " | ".join(cells) + " |")
    lines += [""]

    lines += [
        "## 4. 逐题稳定性",
        "",
        "同一题的判定在多次运行间是否翻转，是判断单次评测可信度的最直接证据。"
        "下表只统计出现翻转的题（同配置内部比较，不含跨配置）：",
        "",
    ]

    # 逐题翻转：用每个 variant 在所有目录里的逐题结果
    items_by_variant: dict[str, dict[str, list[bool]]] = {}
    sql_by_variant: dict[str, dict[str, list[str]]] = {}
    for run_dir in run_dirs:
        data = _load(run_dir)
        for variant, entry in data.items():
            for item in entry.get("items", []):
                items_by_variant.setdefault(variant, {}).setdefault(item["id"], []).append(
                    bool(item["ex"])
                )
                sql_by_variant.setdefault(variant, {}).setdefault(item["id"], []).append(
                    (item.get("sql") or "").strip()
                )

    # SQL 文本在重复运行间的一致性：衡量"temperature=0 到底有多可复现"
    sql_consistent: dict[str, tuple[int, int]] = {}
    for variant, per_q in sql_by_variant.items():
        repeats = [len(v) for v in per_q.values()]
        if not repeats or max(repeats) < 2:
            continue
        stable = sum(1 for values in per_q.values() if len(set(values)) == 1)
        sql_consistent[variant] = (stable, len(per_q))
    sql_note = ""
    if sql_consistent:
        variant, (stable, total_q) = max(
            sql_consistent.items(), key=lambda kv: kv[1][0] / kv[1][1]
        )
        sql_note = (
            f"以 `{variant}` 为例，重复运行间 SQL 文本完全一致的题数是 "
            f"{stable}/{total_q}（{stable / total_q:.0%}）"
        )

    rows = []
    for variant in variants:
        for qid, flags in sorted(items_by_variant.get(variant, {}).items()):
            if len(set(flags)) > 1:
                rows.append((variant, qid, flags))
    if rows:
        lines += ["| 配置 | 题号 | 各次判定 |", "|---|---|---|"]
        for variant, qid, flags in rows:
            marks = " / ".join("对" if f else "错" for f in flags)
            lines.append(f"| {variant} | {qid} | {marks} |")
        lines += [
            "",
            f"共 {len(rows)} 个「配置×题目」出现判定翻转；这些题在单次运行里"
            "被算作正确或错误都带有偶然性，做单题分析时应避开。",
            "",
        ]
    else:
        lines += ["未出现判定翻转。", ""]

    lines += [
        "## 5. 方法学结论（可写进简历/面试）",
        "",
        "1. `temperature=0` 不等于可复现：" + (sql_note or "重复运行间 SQL 文本会变化")
        + "，模型服务端的批处理与路由都会引入噪声。",
        "2. 60 题样本下，单配置 EX 波动约 ±{:.1f}pp，因此**任何小于该量级的消融差距"
        "都不值得解释**，更不该写成因果。".format(noise / 2),
        "3. 若要主张模块有效，正确做法是每配置多次运行取均值并做区间估计，"
        "或把评测集扩到数百题——本报告选择把噪声如实暴露，而不是挑一次好看的数字。",
        "",
        "复现：`" + " ".join(
            [sys.executable.replace("\\", "/"), "scripts/repeatability.py", "--runs"]
        ) + " " + " ".join(str(d) for d in run_dirs) + "`",
        "",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="汇总多次运行，量化评测噪声")
    parser.add_argument(
        "--runs", nargs="+", required=True, help="包含 eval_results.json 的目录列表"
    )
    parser.add_argument("--out", default="docs/03b-重复性检验.md")
    args = parser.parse_args(argv)

    dirs = [Path(d) for d in args.runs]
    collected = _collect(dirs)
    text = build_report(collected, dirs)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")

    for variant, runs in collected.items():
        values = " / ".join(f"{m['ex'] * 100:.1f}%" for m in runs)
        avg = mean(m["ex"] for m in runs)
        print(f"{variant:<12} n={len(runs)} EX {values} 均值 {avg * 100:.1f}%")
    print(f"报告：{out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
