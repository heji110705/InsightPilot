"""评测 CLI：跑一组或多组消融配置，产出 JSON 与 markdown 报告。

用法
----
    D:/Python312/python.exe scripts/run_eval.py --variants full --out docs/
    D:/Python312/python.exe scripts/run_eval.py \
        --variants single,no_schema,no_critic --out docs/

设计取舍
--------
* **结果可叠加**：先跑 full、再跑另外三组时，第二次会读回 ``eval_results.json``
  并按 variant 合并，而不是覆盖——这是"把四组数据补进同一份报告"的实现方式。
  合并前提是 total / limit / mode 一致，否则说明两次跑的评测集不同，直接重建。
* **退出码是防呆，不是评分**：任一 *本次运行* 的 variant EX < 0.3 就返回 1，
  提醒先查引擎/评测链路，而不是把 8% 当成模型效果发出去。报告仍然会写完。
* **打印即证据**：每题一行，包含 EX、attempts、耗时；失败时附一句差异，
  让"数字从哪来"在终端里就能对上。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.agents.pipeline import VARIANTS                     # noqa: E402
from app.eval.report import render_report, write_report      # noqa: E402
from app.eval.runner import load_gold, run_matrix            # noqa: E402

GUARD_EX = 0.3
REPORT_NAME = "03-评测报告.md"
RESULTS_NAME = "eval_results.json"
VARIANT_ORDER = ("full", "no_critic", "no_schema", "single")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="InsightPilot 评测：结果集等价（EX）+ 消融对比",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--variants",
        default="full",
        help=f"逗号分隔的配置，可选：{', '.join(VARIANTS)}",
    )
    parser.add_argument("--limit", type=int, default=None, help="只跑前 N 题（默认全部）")
    parser.add_argument("--out", default="docs/", help="报告与 JSON 输出目录")
    parser.add_argument(
        "--mode",
        choices=("template", "auto"),
        default="template",
        help="template=强制规则推理器；auto=有 Key 走 LLM、无 Key 回落规则",
    )
    parser.add_argument("--sleep", type=float, default=0.0, help="每题之间的限速秒数")
    parser.add_argument(
        "--gold",
        default=None,
        help="评测集路径（默认 data/gold.jsonl）。用 data/gold.v1.jsonl 可复现审计前的分数",
    )
    return parser.parse_args(argv)


def _parse_variants(raw: str) -> list[str]:
    names = [part.strip() for part in (raw or "").split(",") if part.strip()]
    if not names:
        raise SystemExit("[FATAL] --variants 不能为空")
    unknown = [name for name in names if name not in VARIANTS]
    if unknown:
        raise SystemExit(f"[FATAL] 未知配置 {unknown}；可选：{list(VARIANTS)}")
    # 去重但保留用户给的顺序
    return list(dict.fromkeys(names))


def _out_dir(raw: str) -> Path:
    path = Path(raw)
    return path if path.is_absolute() else ROOT / path


def _merge(
    existing: dict[str, dict[str, Any]],
    fresh: dict[str, dict[str, Any]],
) -> tuple[dict[str, dict[str, Any]], bool]:
    """同一评测范围才合并；范围不同就重建，避免 5 题与 60 题混在一张表里。"""
    if not existing:
        return fresh, False
    fresh_metrics = next(iter(fresh.values()))["metrics"]
    scope = (fresh_metrics["total"], fresh_metrics.get("limit"), fresh_metrics.get("mode"))
    for entry in existing.values():
        metrics = entry.get("metrics") or {}
        other = (metrics.get("total"), metrics.get("limit"), metrics.get("mode"))
        if other != scope:
            return fresh, True
    merged = dict(existing)
    merged.update(fresh)
    ordered = {name: merged[name] for name in VARIANT_ORDER if name in merged}
    ordered.update({name: entry for name, entry in merged.items() if name not in ordered})
    return ordered, False


def _progress(variant: str, row: dict[str, Any], *, show_variant: bool) -> None:
    prefix = f"[{variant}] " if show_variant else ""
    line = (
        f"{prefix}{row['id']} {row['type']} EX={int(row['ex'])} "
        f"att={row['attempts']} {row['elapsed_ms']}ms"
    )
    if row["status"] != "ok":
        line += f"  status={row['status']}"
    elif not row["ex"]:
        line += f"  {str(row['diff'])[:44]}"
    print(line, flush=True)


def _print_summary(results: dict[str, dict[str, Any]]) -> None:
    print("")
    print("=" * 78)
    print(f"{'配置':<12}{'EX':>16}{'首答正确':>14}{'平均尝试':>10}{'p50':>9}{'p95':>9}")
    print("-" * 78)
    for name, entry in results.items():
        m = entry["metrics"]
        ex = f"{m['ex'] * 100:.1f}% ({m['ex_count']}/{m['total']})"
        first = f"{m['pass_at_1'] * 100:.1f}%"
        print(
            f"{name:<12}{ex:>16}{first:>14}{m['avg_attempts']:>10.2f}"
            f"{m['p50_ms']:>8}ms{m['p95_ms']:>8}ms"
        )
    print("-" * 78)
    for name, entry in results.items():
        m = entry["metrics"]
        types = " ".join(
            f"{qtype} {row['ex'] * 100:.0f}%" for qtype, row in sorted(m["types"].items())
        )
        print(f"[{name}] 分类型：{types}")
        fallback = m.get("fallback")
        if fallback:
            total = m.get("llm_calls") or m["total"]
            sql_fb = fallback.get("sql_fallback", 0)
            print(
                f"[{name}] 模型回退：SQL {sql_fb}/{total}，"
                f"意图 {fallback.get('intent_fallback', 0)}/{total}，"
                f"结论 {fallback.get('insight_fallback', 0)}/{total}"
            )
    print("=" * 78)


def _command_line(args: argparse.Namespace, variants: str | None = None) -> str:
    exe = sys.executable.replace("\\", "/")
    parts = [
        exe, "scripts/run_eval.py",
        "--variants", variants or args.variants,
        "--out", args.out,
    ]
    if args.limit:
        parts += ["--limit", str(args.limit)]
    if args.mode != "template":
        parts += ["--mode", args.mode]
    if args.sleep:
        parts += ["--sleep", str(args.sleep)]
    return " ".join(parts)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    variants = _parse_variants(args.variants)
    out_dir = _out_dir(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    gold = load_gold(args.gold) if args.gold else load_gold()
    if args.gold:
        print(f"评测集来源：{args.gold}")
    if args.limit is not None:
        if args.limit <= 0:
            print("[FATAL] --limit 必须大于 0", file=sys.stderr)
            return 2
        if args.limit > len(gold):
            print(f"[WARN] --limit={args.limit} 超过题数 {len(gold)}，按全部跑")
            args.limit = len(gold)

    selected = gold[: args.limit] if args.limit else gold
    print("=" * 78)
    print(f"评测集 {len(gold)} 题｜本次 {len(selected)} 题｜配置 {', '.join(variants)}｜模式 {args.mode}")
    print("=" * 78)

    results = run_matrix(
        variants,
        gold,
        limit=args.limit,
        mode=args.mode,
        sleep=args.sleep,
        on_item=lambda variant, row: _progress(
            variant, row, show_variant=len(variants) > 1
        ),
    )

    results_path = out_dir / RESULTS_NAME
    degraded = [
        (name, results[name]["metrics"].get("degraded_reason", ""))
        for name in variants
        if results[name]["metrics"].get("degraded")
    ]
    if degraded:
        # 一条纪律：模型没答上、被规则模板顶替的评测，不许写成报告。
        # 否则 docs/03 会被一份"LLM 模式"的模板结果覆盖，数字是假的而且没人看得出来。
        print("", file=sys.stderr)
        print("=" * 78, file=sys.stderr)
        for name, reason in degraded:
            print(f"[FATAL] {name} 已降级：{reason}", file=sys.stderr)
        print("模型调用大面积回退到规则模板，本次结果等价于模板基线。", file=sys.stderr)
        print("已放弃写出报告与 JSON，避免覆盖上一份可信结果。", file=sys.stderr)
        print("先确认 Key / 网络 / 模型名，再重跑。", file=sys.stderr)
        print("=" * 78, file=sys.stderr)
        return 2

    existing: dict[str, dict[str, Any]] = {}
    if results_path.exists():
        try:
            existing = json.loads(results_path.read_text(encoding="utf-8"))
            if not isinstance(existing, dict):
                raise ValueError("顶层不是对象")
        except Exception as exc:
            print(f"[WARN] 旧结果无法解析，忽略：{exc}")
            existing = {}

    merged, rebuilt = _merge(existing, results)
    if rebuilt:
        print("[WARN] 本次与旧结果的题数/limit/mode 不一致，报告将只含本次配置")
    elif existing:
        print(f"[INFO] 已与旧结果合并，报告包含 {len(merged)} 组配置")

    results_path.write_text(
        json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    report_text = render_report(
        merged, command=_command_line(args, variants=",".join(merged))
    )
    report_path = write_report(out_dir / REPORT_NAME, report_text)

    _print_summary(merged)
    print(f"JSON：{results_path}")
    print(f"报告：{report_path}")

    guard_hits = [
        (name, results[name]["metrics"]["ex"])
        for name in variants
        if results[name]["metrics"]["ex"] < GUARD_EX
    ]
    if guard_hits:
        detail = "、".join(f"{name} {ex * 100:.1f}%" for name, ex in guard_hits)
        print("")
        print(
            f"[GUARD] {detail} 低于 {GUARD_EX * 100:.0f}% 防呆线；"
            f"先排查引擎与评测链路，不要把这些数字当模型效果（见报告第 5 节）。"
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
