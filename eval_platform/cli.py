"""评测中台 · 命令行入口

    python -m eval_platform list                      看有哪些 run
    python -m eval_platform show RUN                   看一个 run 的指标
    python -m eval_platform diff A B                   两个 run 逐题对比
    python -m eval_platform run --tag v7 --limit 20    跑一次新评测（会打线上，花钱）
    python -m eval_platform score RUN                  对已采集的 run 打 RAGAS（免费，可重跑）
    python -m eval_platform gate BASE NEW              门禁：可答率掉超过阈值就 exit 1
    python -m eval_platform report                     出一张 HTML 看板

约定：
  * 只读线上，写只写自己的工作目录，不碰 projects/ 里任何代码
  * run 文件默认落在 <root>/eval_runs/，旧的在 _p1/runs/，两边都能读
  * `run` 与 `score` 刻意分开：采集花钱、评分免费。绑在一起等于改一次评分口径
    就要重打一遍线上
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import compare, gate, ragas_score, runner, schema, suites

# 工作区根：本文件在 <root>/eval_platform/cli.py
ROOT = Path(__file__).resolve().parent.parent

# 找 run 的默认目录，先新后旧
SCAN_DIRS = ["eval_runs"]   # 仓库里没有 _p1/runs（那是作者工作区的历史目录）

#: baseline 是人工维护的锚点。CI 只读它，任何自动产物都不许落在这个文件名上，
#: 否则门禁会自己把自己校准成"永远绿"。
BASELINE_NAME = "baseline.json"


def _iter_run_files(extra: list[str] | None = None) -> list[Path]:
    out: list[Path] = []
    seen: set[Path] = set()
    for d in SCAN_DIRS:
        p = ROOT / d
        if p.is_dir():
            for f in sorted(p.glob("*.json")):
                if f.resolve() not in seen:
                    out.append(f)
                    seen.add(f.resolve())
    for e in extra or []:
        p = Path(e)
        if p.is_dir():
            for f in sorted(p.glob("*.json")):
                if f.resolve() not in seen:
                    out.append(f)
                    seen.add(f.resolve())
        elif p.is_file() and p.resolve() not in seen:
            out.append(p)
            seen.add(p.resolve())
    return out


def _resolve(name: str) -> Path:
    """允许用文件名片段定位 run，省得敲全路径。

    同一个前缀常常命中好几个（005140 会同时命中 005140.json、005140_scored.json、
    005140_ar.json）。派生文件（_scored / _ar）是评分产物，未必带逐题数据，
    diff 用不了，所以优先挑「能读且有 items」的那个，长度短的优先。
    """
    p = Path(name)
    if p.exists():
        return p
    hits = [f for f in _iter_run_files() if name in f.name]
    if not hits:
        sys.exit(f"找不到 run：{name}")

    def rank(f: Path) -> tuple:
        try:
            has_items = 1 if schema.load(f).items else 0
        except Exception:  # noqa: BLE001 — 坏文件排最后
            has_items = 0
        return (has_items, -len(f.name))

    hits.sort(key=rank, reverse=True)
    if len(hits) > 1:
        print(f"匹配到 {len(hits)} 个，用第一个：")
        for h in hits:
            print("   ", h.name)
    return hits[0]


# ------------------------------------------------------------------ 子命令

def cmd_list(args) -> None:
    files = _iter_run_files(args.dir)
    if not files:
        sys.exit("没有任何 run 文件")
    print(f"{'文件':44s} {'套件':13s} {'题数':>5s} {'可答率':>8s} {'p50':>9s} {'落盘时间':>17s}")
    print("-" * 106)
    for f in files:
        try:
            r = schema.load(f)
        except Exception as e:  # noqa: BLE001 — 坏文件不该让 list 整个挂掉
            print(f"{f.name:44s}  读不了：{type(e).__name__}")
            continue
        m = r.metrics()
        rate = m.get("answerable_rate")
        p50 = m.get("latency_p50_ms")
        print(
            f"{f.name:44s} {r.suite:13s} {m.get('n') or 0:>5d} "
            f"{(f'{rate * 100:.2f}%' if rate is not None else '—'):>8s} "
            f"{(f'{int(p50):,}' if p50 else '—'):>9s} {r.created_at:>17s}"
        )


def cmd_show(args) -> None:
    r = schema.load(_resolve(args.run))
    m = r.metrics()
    print(f"套件 {r.suite}   标记 {r.tag or '-'}   时间 {r.created_at}")
    print(f"来源 {r.source}")
    if r.config:
        print(f"配置 {r.config}")
    print("-" * 60)
    for k, v in m.items():
        if k == "ragas":
            continue
        print(f"  {k:24s}{v}")
    if m.get("ragas"):
        print("  ragas")
        for k, v in m["ragas"].items():
            print(f"    {k:30s}{v}")


def cmd_diff(args) -> None:
    a = schema.load(_resolve(args.a))
    b = schema.load(_resolve(args.b))
    if a.suite != b.suite:
        print(f"⚠ 两个 run 的套件不同（{a.suite} vs {b.suite}），指标不可比；"
              f"下面只列逐题的「对/错」，不要横向比数字。\n")
    if not a.items or not b.items:
        sys.exit("两个 run 都要有逐题数据才能 diff（*_scored.json 那种汇总文件不行）")
    print(compare.diff_text(a, b, verbose=args.verbose))


def cmd_run(args) -> None:
    s = suites.get(args.suite)
    base = args.base_url or s.base_url
    # 先把落点检查一遍再花钱：`--out eval_runs/baseline.json` 被拒绝时，
    # 不该已经打完 20 题才报错。
    _reject_baseline_target(args.out)
    if not args.yes:
        print("这会真打线上接口并产生费用。确认加 --yes")
        print(f"  套件 {s.name}   接口 {base}{s.ask_path}")
        print(f"  题目 {args.limit or '全量'} 道   判据 {s.spec}")
        sys.exit(1)
    run = runner.collect(
        root=ROOT,
        suite=s.name,
        base_url=args.base_url,
        mode=args.mode,
        gold=args.gold,
        limit=args.limit,
        sleep=args.sleep,
        timeout=args.timeout,
        api_key=args.api_key,
        tag=args.tag,
        only=args.only,
        quiet=args.quiet,
    )
    out = _out_path(args.out, run)
    schema.save(run, out)
    m = run.metrics()
    print(f"\n落盘 → {out}")
    print(f"可答率 {_pct(m.get('answerable_rate'))}  拒答 {_pct(m.get('refuse_accuracy'))}  "
          f"列覆盖 {_pct(m.get('cover_rate'))}  p50 {m.get('latency_p50_ms')}ms  "
          f"单题 {_money(m.get('cost_per_q_cny'))}")
    if m.get("n_error"):
        print(f"⚠ 失败 {m['n_error']} 题（不进分母，diff 前先确认不是链路问题）")


def _reject_baseline_target(raw: str) -> None:
    """`--out` 指向 baseline.json 时直接退出。

    这是一个人为的护栏：baseline 一旦被自动产物覆盖，门禁就永远绿了——
    它比的会是"刚跑出来的自己"。确认是有意变更后，人工 `cp` 过去。
    """
    if Path(raw).suffix.lower() == ".json" and Path(raw).name == BASELINE_NAME:
        sys.exit(f"{BASELINE_NAME} 只能人工更新：CI 的产物不许覆盖 baseline，"
                 f"否则门禁会把自己校准成永远绿")


def _out_path(raw: str, run: schema.Run) -> Path:
    """`--out` 可以给目录，也可以给一个 .json 路径。

    CI 用固定文件名（`eval_runs/ci-latest.json`）才能稳定地交给 gate；
    人工跑则用时间戳目录名，避免互相覆盖。
    """
    p = Path(raw)
    if p.suffix.lower() == ".json":
        _reject_baseline_target(raw)
        out = p if p.is_absolute() else ROOT / p
        out.parent.mkdir(parents=True, exist_ok=True)
        return out
    return runner.default_out(ROOT, run, raw)


def cmd_score(args) -> None:
    path = _resolve(args.run)
    run = schema.load(path)
    if not run.items:
        sys.exit(f"{path.name} 没有逐题数据，没法评分（*_scored.json 那种汇总文件不行）")

    try:
        agg = ragas_score.score_run(
            run,
            model=args.ragas_model,
            base=args.ragas_base,
            key=args.ragas_key,
            limit=args.limit,
            progress=not args.quiet,
        )
    except ragas_score.NoSamples as e:
        # 3 = 没样本可评（这个 run 本身没东西可打，不是故障）
        print(f"[skip] {e}")
        sys.exit(3)
    except ragas_score.JudgeEndpoint as e:
        # 5 = 依赖装好了，但裁判端点不通。与「依赖没装」分开：
        # 这两种失败以前都是一句 "[skip]" + exit 3，光看日志分不出是哪一种。
        print(f"[fail] {e}")
        sys.exit(5)
    except ragas_score.RagasUnavailable as e:
        # 4 = 依赖没装 / judge 构造失败
        print(f"[fail] {e}")
        sys.exit(4)

    # 汇总里除了均值还有"这批判分是怎么来的"：裁判模型、样本量、口径说明。
    # 它们不进 metrics（metrics 只算能从 items 算出来的东西），放在 config 里。
    run.config["ragas"] = agg
    out = _out_path(args.out, run) if args.out else path
    schema.save(run, out)

    print("\n=== RAGAS ===")
    for k, v in agg.items():
        print(f"  {k}: {v}")
    print(f"\n落盘 → {out}")
    print(f"逐题分已写进 items[*].ragas，metrics() 的 ragas 段会自动跟着算")


def cmd_gate(args) -> None:
    base_path = Path(args.baseline)
    if not base_path.is_absolute():
        base_path = ROOT / base_path
    new_path = Path(args.new)
    if not new_path.is_absolute():
        new_path = ROOT / new_path
    for p in (base_path, new_path):
        if not p.exists():
            sys.exit(f"找不到 run：{p}")

    baseline = schema.load(base_path)
    new = schema.load(new_path)
    if args.max_drop_n is not None and args.max_drop_n < 1:
        sys.exit("--max-drop-n 至少为 1（「一题都不许掉」也用 1，0 会把所有 run 判红）")
    ok, text = gate.check(baseline, new,
                          max_drop_pp=args.max_drop,
                          max_drop_n=args.max_drop_n,
                          min_valid_ratio=args.min_valid_ratio)
    print(text)
    sys.exit(0 if ok else 1)


def cmd_report(args) -> None:
    files = _iter_run_files(args.dir)
    runs, skipped = [], 0
    for f in files:
        try:
            r = schema.load(f)
        except Exception:  # noqa: BLE001
            continue
        if not r.items:  # 只有带逐题数据的才进看板
            continue
        if args.suite and r.suite != args.suite:
            skipped += 1
            continue
        if args.tag_prefix and not (r.tag or "").startswith(args.tag_prefix):
            continue
        runs.append(r)
    if not runs:
        sys.exit(f"没有带逐题数据的 {args.suite or ''} run".strip())
    if skipped:
        print(f"已跳过 {skipped} 个其它套件的 run（看板不混两种判据；用 --suite 指定）")
    if len({r.suite for r in runs}) > 1:
        sys.exit("候选里混了多个套件，请用 --suite 指定一个（可答率的含义不同，不能画在同一条线上）")
    out = Path(args.out)
    if not out.is_absolute():
        out = ROOT / out
    out.parent.mkdir(parents=True, exist_ok=True)
    title = args.title or ("InsightPilot 评测看板" if runs[0].suite == "insightpilot"
                           else "DocuMind 评测看板")
    out.write_text(compare.report_html(runs, title, args.min_n, args.max_error), encoding="utf-8")
    print(f"看板 → {out}   （候选 {len(runs)} 个 run，浏览器直接打开）")


def _pct(v) -> str:
    return f"{v * 100:.2f}%" if v is not None else "—"


def _money(v) -> str:
    return f"¥{v:.5f}" if v is not None else "—"


# ------------------------------------------------------------------ 入口

def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="eval_platform",
        description="DocuMind / InsightPilot 评测中台",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("list", help="列出所有 run")
    p.add_argument("--dir", action="append", help="额外的 run 目录（可重复）")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("show", help="看一个 run 的指标")
    p.add_argument("run")
    p.set_defaults(func=cmd_show)

    p = sub.add_parser("diff", help="两个 run 逐题对比")
    p.add_argument("a")
    p.add_argument("b")
    p.add_argument("-v", "--verbose", action="store_true", help="列出全部题号")
    p.set_defaults(func=cmd_diff)

    p = sub.add_parser("run", help="跑一次新评测（打线上，花钱）")
    p.add_argument("--suite", default="documind", choices=sorted(suites.SUITES),
                   help="documind=关键词判据；insightpilot=结果集等价 + 列覆盖")
    p.add_argument("--base-url", default=None, help="默认取套件内置地址")
    p.add_argument("--mode", default="mix", help="仅 documind 有意义（mix/naive/local/global/hybrid）")
    p.add_argument("--gold", default=None)
    p.add_argument("--limit", type=int, default=0, help="0 = 全量")
    p.add_argument("--only", default="", help="只跑某一题型（documind: fact/refuse，insightpilot: T1..T6）")
    p.add_argument("--sleep", type=float, default=None,
                   help="每题间隔秒；默认取套件内置值（documind 2s，insightpilot 21s——防刷闸门是 3 次/分）")
    p.add_argument("--timeout", type=float, default=180.0)
    p.add_argument("--api-key", default="")
    p.add_argument("--tag", default="", help="给这次 run 打个标记，如 v7.3 或 ci-abc1234")
    p.add_argument("--out", default="eval_runs",
                   help="目录，或一个 .json 路径（CI 用固定文件名才方便交给 gate）")
    p.add_argument("--quiet", action="store_true")
    p.add_argument("--yes", action="store_true", help="确认会产生费用")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("score", help="对已采集的 run 打 RAGAS（免费，可重跑）")
    p.add_argument("run")
    p.add_argument("--limit", type=int, default=0, help="只给前 N 题打分（0 = 全部）")
    p.add_argument("--ragas-model", default=ragas_score.DEFAULT_MODEL)
    p.add_argument("--ragas-base", default=None)
    p.add_argument("--ragas-key", default=None)
    p.add_argument("--out", default="", help="写到哪里，默认原地写回（评分可重跑）")
    p.add_argument("--quiet", action="store_true", help="不显示 ragas 进度条")
    p.set_defaults(func=cmd_score)

    p = sub.add_parser("gate", help="门禁：可答率掉超过阈值就 exit 1")
    p.add_argument("baseline", help="基线，仓库里的 eval_runs/baseline.json")
    p.add_argument("new", help="这次的新 run")
    # 两种阈值互斥：20 题这种小样本上 pp 没有意义（掉 1 题就是 6.25pp），
    # 所以 CI 用「掉几题」，样本量够大时才用 pp。
    drop = p.add_mutually_exclusive_group()
    drop.add_argument("--max-drop", type=float, default=None,
                      help="允许跌多少 pp（不传且不给 --max-drop-n 时按 2.0）")
    drop.add_argument("--max-drop-n", type=int, default=None,
                      help="掉满几题就红（20 题的 CI 用 2：掉 1 题过、掉 2 题失败）")
    p.add_argument("--min-valid-ratio", type=float, default=0.8,
                   help="新 run 的有效题数不得低于 baseline 的这个比例")
    p.set_defaults(func=cmd_gate)

    p = sub.add_parser("report", help="生成 HTML 看板")
    p.add_argument("--dir", action="append")
    p.add_argument("--suite", default="documind", choices=["", *sorted(suites.SUITES)],
                   help="只看某个套件（两种判据不能画在同一条线上）")
    p.add_argument("--out", default="eval_runs/report.html")
    p.add_argument("--title", default="")
    p.add_argument("--tag-prefix", default="", help="只看某个前缀的 run")
    p.add_argument("--min-n", type=int, default=20, help="样本量下限，低于此不进看板")
    p.add_argument("--max-error", type=float, default=0.2, help="失败率上限，高于此不进看板")
    p.set_defaults(func=cmd_report)

    return ap


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
