"""只重渲染报告，不重跑评测。

用途：`run_eval.py` 在进程启动时就 import 了 `app.eval.report`，如果评测跑到一半
改了报告模板的文案，最终写出的仍是旧模板。这个脚本从 `eval_results.json` 重新
渲染，保证报告与当前代码一致，且不消耗一次模型调用。

    D:/Python312/python.exe scripts/render_report.py --out docs/
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.eval.report import write_report, render_report  # noqa: E402

REPORT_NAME = "03-评测报告.md"
RESULTS_NAME = "eval_results.json"
DEFAULT_CMD = "python scripts/run_eval.py --variants <见各配置> --mode auto --out docs/"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="从 eval_results.json 重渲染评测报告")
    parser.add_argument("--out", default="docs/", help="报告目录（含 eval_results.json）")
    parser.add_argument("--command", default="", help="写进报告的复现命令")
    args = parser.parse_args(argv)

    out_dir = Path(args.out)
    results_path = out_dir / RESULTS_NAME
    if not results_path.exists():
        print(f"[FATAL] 找不到 {results_path}", file=sys.stderr)
        return 2

    results = json.loads(results_path.read_text(encoding="utf-8"))
    if not isinstance(results, dict) or not results:
        print("[FATAL] eval_results.json 为空或结构不对", file=sys.stderr)
        return 2

    command = args.command or DEFAULT_CMD
    text = render_report(results, command=command)
    path = write_report(out_dir / REPORT_NAME, text)

    for name, entry in results.items():
        m = entry["metrics"]
        print(
            f"{name:<12} mode={m.get('mode'):<5} EX={m['ex'] * 100:5.1f}% "
            f"({m['ex_count']}/{m['total']}) 首答={m['pass_at_1'] * 100:5.1f}%"
        )
    print(f"报告：{path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
