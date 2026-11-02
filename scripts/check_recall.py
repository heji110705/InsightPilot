"""语义层召回体检：60 道 gold 题里用到的物理表，是否都被检索器放进了上下文。

评测的是"语义层内容够不够"，不是检索器算法：命中率低就去补 synonyms.yml 的
别名或表卡片的 meaning，不改 app/semantic/*（那是 Agent D 的模块）。

判定口径：gold SQL 经 guard 解析出的每一张物理表（含视图），都必须出现在
`search(question, budget=2600)` 返回的 `context.tables` 里。少一张就意味着
下游 SQLCraft 看不到 schema，必然编不出这条 SQL。

用法：D:/Python312/python.exe scripts/check_recall.py
"""
from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core.config import SETTINGS                      # noqa: E402
from app.db.guard import validate_sql                    # noqa: E402
from app.db.repo import WarehouseRepo                    # noqa: E402
from app.semantic.loader import load_semantic             # noqa: E402
from app.semantic.retriever import SemanticRetriever      # noqa: E402

GOLD_PATH = ROOT / "data" / "gold.jsonl"
BUDGET = 2600
TARGET = 0.90


def main() -> int:
    gold = [json.loads(line) for line in
            GOLD_PATH.read_text(encoding="utf-8").splitlines() if line.strip()]

    bundle = load_semantic(SETTINGS.semantic_dir)
    retriever = SemanticRetriever(bundle)
    repo = WarehouseRepo(SETTINGS.db_path, SETTINGS.row_limit, SETTINGS.query_timeout_ms)
    allowed = repo.table_names()

    print("=" * 72)
    print(f"语义层：{len(bundle.tables)} 表 / {len(bundle.metrics)} 指标 / "
          f"{len(bundle.fewshots)} 范例 / {len(bundle.synonyms)} 同义词组 / "
          f"{len(bundle.join_hints)} JOIN 提示")
    print(f"召回体检：{len(gold)} 题，budget={BUDGET}，目标命中率 >= {TARGET:.0%}")
    print("=" * 72)

    hits = 0
    misses: list[tuple[str, list[str], list[str]]] = []
    missing_table_counter: Counter[str] = Counter()
    kept_table_counter: Counter[str] = Counter()

    for item in gold:
        qid, question, sql = item["id"], item["question"], item["sql"]
        verdict = validate_sql(sql, allowed, SETTINGS.row_limit)
        need = set(verdict.tables)
        if not verdict.ok:
            print(f"[WARN] {qid} gold SQL 未过守卫，跳过：{verdict.reason}")
            misses.append((qid, sorted(need), ["<guard-failed>"]))
            continue

        ctx = retriever.search(question, budget=BUDGET)
        got = {t.name for t in ctx.tables}
        for name in got:
            kept_table_counter[name] += 1
        missing = sorted(need - got)
        if missing:
            misses.append((qid, sorted(need), missing))
            missing_table_counter.update(missing)
        else:
            hits += 1

    rate = hits / len(gold) if gold else 0.0
    print(f"\n命中 {hits}/{len(gold)}　命中率 {rate:.1%}")

    if misses:
        print("\n[未命中清单]")
        print(f"  {'题号':<5}{'gold 需要的表':<52}{'缺的表'}")
        for qid, need, missing in misses:
            print(f"  {qid:<5}{', '.join(need)[:50]:<52}{', '.join(missing)}")
        print("\n[缺失表频次]")
        for name, cnt in missing_table_counter.most_common():
            print(f"  {name:<18}{cnt} 次")

    print("\n[每张表被召回的题数 / 60]")
    for card in bundle.tables:
        print(f"  {card.name:<18}{kept_table_counter.get(card.name, 0):>3}")

    print("\n" + "=" * 72)
    if rate < TARGET:
        print(f"结论：FAIL　命中率 {rate:.1%} < 目标 {TARGET:.0%}，请补 synonyms.yml 或表卡片 meaning")
        return 1
    print(f"结论：PASS　命中率 {rate:.1%} >= 目标 {TARGET:.0%}（未命中 {len(misses)} 题）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
