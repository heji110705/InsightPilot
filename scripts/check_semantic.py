"""语义层自检：条数、槽位、模板可执行性、范例可执行性。

契约只规定"要有槽位"，没规定"槽位填空串要能跑"。但下游 TemplateInferencer
就是按"没条件就不填"的方式拼 SQL 的，模板只要有一个空槽位拼不出合法 SQL，
兜底链路就整条失效。所以这里把这条隐式约定变成显式检查。

用法：D:/Python312/python.exe scripts/check_semantic.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core.config import SETTINGS                      # noqa: E402
from app.db.repo import QueryFailed, WarehouseRepo        # noqa: E402
from app.semantic.loader import load_semantic             # noqa: E402

SLOTS = ("{where}", "{group}", "{having}", "{order}", "{limit}")
EXPECT = {"tables": 8, "metrics": 20, "fewshots": 24, "synonyms": 20}


def main() -> int:
    bundle = load_semantic(SETTINGS.semantic_dir)
    repo = WarehouseRepo(SETTINGS.db_path, SETTINGS.row_limit, SETTINGS.query_timeout_ms)
    failures: list[str] = []

    counts = {
        "tables": len(bundle.tables),
        "metrics": len(bundle.metrics),
        "fewshots": len(bundle.fewshots),
        "synonyms": len(bundle.synonyms),
    }
    print("=" * 72)
    print("语义层自检")
    print("=" * 72)
    for key, want in EXPECT.items():
        got = counts[key]
        flag = "OK" if got >= want else "FAIL"
        print(f"  {key:<10}{got:>4}  期望 >= {want:<3}{flag}")
        if got < want:
            failures.append(f"{key} 只有 {got}，期望 >= {want}")

    empty_cell = []
    print("\n[指标模板：五个槽位 + 空串直跑]")
    for metric in bundle.metrics:
        missing = [slot for slot in SLOTS if slot not in metric.sql_template]
        if missing:
            failures.append(f"{metric.key} 缺槽位 {missing}")
            continue
        sql = metric.sql_template.format(where="", group="", having="", order="", limit="")
        try:
            result = repo.run_query(sql)
        except QueryFailed as exc:
            failures.append(f"{metric.key} 空槽位执行失败：{exc.message}")
            print(f"  {metric.key:<28} FAIL {exc.message[:60]}")
            continue
        if result.rowcount == 0:
            failures.append(f"{metric.key} 空槽位返回 0 行")
        if result.rowcount == 1 and len(result.columns) == 1:
            empty_cell.append(metric.key)
        print(f"  {metric.key:<28} rows={result.rowcount}")

    print("\n[范例：逐条执行]")
    for idx, shot in enumerate(bundle.fewshots, 1):
        try:
            result = repo.run_query(shot.sql)
        except QueryFailed as exc:
            failures.append(f"fewshot#{idx} 执行失败：{exc.message}")
            print(f"  #{idx:<3} FAIL {exc.message[:70]}")
            continue
        if result.rowcount == 0:
            failures.append(f"fewshot#{idx} 返回 0 行：{shot.question}")
        print(f"  #{idx:<3} rows={result.rowcount:<4} {shot.question[:40]}")

    print("\n" + "=" * 72)
    if failures:
        print(f"结论：FAIL（{len(failures)} 项）")
        for line in failures:
            print(f"  - {line}")
        return 1
    print("结论：PASS　条数达标，20 个模板空槽位可直跑，24 条范例全部有数据")
    print(f"　单行单列的指标：{', '.join(empty_cell)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
