"""gold.jsonl 体检脚本：守卫、可执行性、区分度、分类型统计。

四件事按顺序做，任何一件不通过都以非 0 退出码结束——评测集是整条链路的
地基，静默跳过一条错 SQL 等于给自己发一张假的成绩单。

1. 60 条全部能过 `validate_sql`（白名单取仓库真实对象名，含视图）；
2. 每条实际执行且返回行数 > 0（0 行的题要么题错了要么 SQL 错了）；
3. 统计结果集形状，标出"1 行 1 列"这种区分度极低的题，超过 15 条要提醒；
4. 打印 T1~T6 的题目数与平均结果行数。

用法：D:/Python312/python.exe scripts/check_gold.py
"""
from __future__ import annotations

import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core.config import SETTINGS                      # noqa: E402
from app.db.guard import validate_sql                    # noqa: E402
from app.db.repo import QueryFailed, WarehouseRepo       # noqa: E402

GOLD_PATH = ROOT / "data" / "gold.jsonl"
TYPES = ("T1", "T2", "T3", "T4", "T5", "T6")
REQUIRED = ("id", "type", "question", "sql", "note")
SINGLE_CELL_WARN = 15


def load_gold(path: Path) -> list[dict]:
    """读 JSONL：空行跳过，结构错误直接抛，不吞。"""
    rows: list[dict] = []
    for lineno, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError as exc:
            raise SystemExit(f"[FATAL] 第 {lineno} 行不是合法 JSON：{exc}") from exc
        missing = [key for key in REQUIRED if not str(item.get(key, "")).strip()]
        if missing:
            raise SystemExit(f"[FATAL] 第 {lineno} 行缺字段 {missing}")
        rows.append(item)
    return rows


def main() -> int:
    gold = load_gold(GOLD_PATH)
    repo = WarehouseRepo(SETTINGS.db_path, SETTINGS.row_limit, SETTINGS.query_timeout_ms)
    allowed = repo.table_names()

    print("=" * 72)
    print(f"gold 体检：{GOLD_PATH}")
    print(f"题目数 {len(gold)}｜可查对象 {len(allowed)} 个：{', '.join(sorted(allowed))}")
    print("=" * 72)

    failures: list[str] = []          # 阻断项
    guard_fail: list[tuple[str, str]] = []
    empty_rows: list[tuple[str, str]] = []
    single_cell: list[str] = []
    per_type: dict[str, list[int]] = defaultdict(list)
    per_type_questions: dict[str, list[str]] = defaultdict(list)
    id_counter = Counter(item["id"] for item in gold)

    duplicates = [qid for qid, cnt in id_counter.items() if cnt > 1]
    if duplicates:
        failures.append(f"题号重复：{duplicates}")
    unknown_types = sorted({item["type"] for item in gold} - set(TYPES))
    if unknown_types:
        failures.append(f"未知题型：{unknown_types}")

    for item in gold:
        qid, qtype, sql = item["id"], item["type"], item["sql"]
        per_type_questions[qtype].append(qid)

        verdict = validate_sql(sql, allowed, SETTINGS.row_limit)
        if not verdict.ok:
            guard_fail.append((qid, verdict.reason))
            failures.append(f"{qid} 未过守卫：{verdict.reason}")
            per_type[qtype].append(0)
            continue

        try:
            result = repo.run_query(sql)
        except QueryFailed as exc:
            empty_rows.append((qid, f"执行失败[{exc.stage}]：{exc.message}"))
            failures.append(f"{qid} 执行失败：{exc.message}")
            per_type[qtype].append(0)
            continue

        rowcount = result.rowcount
        per_type[qtype].append(rowcount)
        if rowcount == 0:
            empty_rows.append((qid, "返回 0 行"))
            failures.append(f"{qid} 返回 0 行")
        if rowcount == 1 and len(result.columns) == 1:
            single_cell.append(qid)

    # ---- 报告 ----------------------------------------------------------
    def section(title: str, lines: list[str]) -> None:
        print(f"\n[{title}]")
        if not lines:
            print("  OK")
            return
        for line in lines:
            print(f"  {line}")

    section("1. 守卫校验", [f"{qid}: {reason}" for qid, reason in guard_fail])
    section("2. 结果非空", [f"{qid}: {reason}" for qid, reason in empty_rows])
    section(
        "3. 区分度（1 行 1 列）",
        [f"{qid}" for qid in single_cell]
        + ([f"警告：{len(single_cell)} 条只有 1 行 1 列（阈值 {SINGLE_CELL_WARN}），区分度偏低"]
           if len(single_cell) > SINGLE_CELL_WARN else []),
    )

    print("\n[4. 分类型统计]")
    print(f"  {'类型':<4}{'题数':>4}{'平均结果行数':>14}{'最小':>6}{'最大':>6}")
    for qtype in TYPES:
        counts = per_type.get(qtype, [])
        if not counts:
            print(f"  {qtype:<4}{0:>4}{'-':>14}{'-':>6}{'-':>6}")
            failures.append(f"{qtype} 没有题目")
            continue
        avg = sum(counts) / len(counts)
        print(f"  {qtype:<4}{len(counts):>4}{avg:>14.1f}{min(counts):>6}{max(counts):>6}")
    outside = set(per_type_questions) - set(TYPES)
    if outside:
        failures.append(f"存在不在 T1~T6 的题型：{sorted(outside)}")

    print("\n" + "=" * 72)
    if failures:
        print(f"结论：FAIL（{len(failures)} 项）")
        for line in failures:
            print(f"  - {line}")
        return 1
    print("结论：PASS　60 条全部通过守卫、全部有数据、题号与题型齐全")
    print(f"　1 行 1 列：{len(single_cell)} 条（阈值 {SINGLE_CELL_WARN}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
