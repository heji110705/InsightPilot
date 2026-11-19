"""套件定义：一套评测要用到的接口地址、金标、取样方式、判据。

为什么要有这一层
----------------
`run` 以前只会打 DocuMind 的 `/v1/ask`，判据是关键词命中。InsightPilot 是另一回事：
接口是 `/api/ask`、金标是 60 条 JSONL、判据是**结果集等价 + 列覆盖**。把差异塞进
`if suite == ...` 的十处分支里，第一次改动就会漏掉一处；所以集中成一张表：

    suite          接口                   金标                        判据
    documind       /v1/ask                124 题（94 可答 + 30 必拒）  关键词组命中 + 拒答
    insightpilot   /api/ask               60 题（T1..T6 各 10）        结果集等价 / 列覆盖

判据本身不在这里：关键词在 `keyword.py`，结果集在 `resultset.py`，两边都有单测钉着。

金标与真值
----------
DocuMind 的金标自带 `must_include` 关键词组，判分不需要连库。
InsightPilot 的金标是标准 SQL，得先把它执行一次拿到真值结果集——`load_gold_results`
用**只读** sqlite3 连接本地 `warehouse.sqlite`（该文件在仓库里，CI 上也在），
不 import InsightPilot 的任何模块，也不往库里写一个字节。
"""

from __future__ import annotations

import json
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .keyword import hit
from .resultset import ResultSet, compare, compare_covering

__all__ = [
    "SUITES",
    "Suite",
    "get",
    "load_gold",
    "sample",
    "build_body",
    "parse",
    "judge",
    "load_gold_results",
]


@dataclass(frozen=True)
class Suite:
    """一个套件。

    `kind` 决定判据族：`keyword`（DocuMind）或 `resultset`（InsightPilot）。
    `gold_candidates` / `db_candidates` 是**相对工作区根**的候选路径，先命中先用：
    这样同一份代码在「工作区」（projects/xxx/...）和「仓库根」（data/...）都能跑。
    """
    name: str
    kind: str
    base_url: str
    ask_path: str
    gold_candidates: tuple[str, ...]
    default_sleep: float
    db_candidates: tuple[str, ...] = ()
    spec: str = ""
    model: str = ""


DOCUMIND = Suite(
    name="documind",
    kind="keyword",
    base_url="http://8.163.90.97",
    ask_path="/v1/ask",
    gold_candidates=("projects/documind/eval_set_v2.json", "eval_set_v2.json"),
    default_sleep=2.0,
    spec="关键词组命中（组间 AND、组内 OR）+ 必拒题必须拒答且不编造",
)

# 线上默认 sleep 21s：公开 Demo 有「每 IP 每分钟 3 次」的防刷闸门
# （projects/insightpilot/app/limits.py），打太快会拿到 429，那会把限流误读成
# 「答错了」。CI 打本地实例时可以显式 --sleep 0.5 把它降下来。
INSIGHT_PILOT = Suite(
    name="insightpilot",
    kind="resultset",
    base_url="http://8.163.90.97:8001",
    ask_path="/api/ask",
    gold_candidates=("projects/insightpilot/data/gold.jsonl", "data/gold.jsonl"),
    db_candidates=("projects/insightpilot/data/warehouse.sqlite", "data/warehouse.sqlite"),
    default_sleep=21.0,
    spec="结果集等价（列名/行序/别名不参与，数值 1e-6 相对容差）+ 列覆盖",
)

SUITES: dict[str, Suite] = {s.name: s for s in (DOCUMIND, INSIGHT_PILOT)}


def get(name: str) -> Suite:
    try:
        return SUITES[name]
    except KeyError:
        sys.exit(f"未知套件 {name!r}，可选：{', '.join(SUITES)}")


# ------------------------------------------------------------------ 金标

def load_gold(suite: Suite, explicit: str | None, root: Path) -> tuple[Path, list[dict]]:
    """读金标并**统一成同一形状**：{id, type, question, must_include, sql}。

    两种文件格式（JSON 对象 / JSONL）在各自的分支里消化，下游只认这一种形状。
    """
    path = _find_gold(suite, explicit, root)
    raw: list[dict]
    if path.suffix == ".jsonl":
        raw = []
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            line = line.strip()
            if not line:
                continue
            try:
                raw.append(json.loads(line))
            except json.JSONDecodeError as exc:
                sys.exit(f"金标第 {lineno} 行不是合法 JSON：{exc}")
    else:
        doc = json.loads(path.read_text(encoding="utf-8"))
        raw = doc["items"] if isinstance(doc, dict) else doc

    items = []
    for r in raw:
        q = r.get("q") or r.get("question") or ""
        if not q or not r.get("id"):
            sys.exit(f"金标有条目缺 id 或问题：{r}")
        items.append({
            "id": r["id"],
            "type": r.get("type") or "fact",
            "question": q,
            "must_include": r.get("must_include") or [],
            "must_absent": r.get("must_absent") or [],
            "task": r.get("task") or r.get("note") or "",
            "sql": r.get("sql") or "",
        })
    if not items:
        sys.exit(f"金标为空：{path}")
    return path, items


def _find_gold(suite: Suite, explicit: str | None, root: Path) -> Path:
    if explicit:
        p = Path(explicit)
        if not p.is_absolute():
            p = root / p
        if not p.exists():
            sys.exit(f"找不到金标：{p}")
        return p
    for rel in suite.gold_candidates:
        cand = root / rel
        if cand.exists():
            return cand
    sys.exit(f"找不到 {suite.name} 的金标（找过：{', '.join(suite.gold_candidates)}），"
             f"请用 --gold 指定")


# ------------------------------------------------------------------ 取样

def sample(suite: Suite, items: list[dict], limit: int, only: str = "") -> list[dict]:
    """按题型分层 + **层内等距**取样，避免 `--limit` 把某一类题整类截掉或全取头几题。

    为什么"层内等距"是必须的
    ------------------------
    金标是**按题型分块排列**的（DocuMind 前 16 题全是 schema/口径/陷阱题，
    InsightPilot 是 T1 十题、T2 十题……）。直接取前 N 会得到一个偏难的子集：
    实测 20 题取头 16 个 fact 只有 68.75%，而全量 124 题是 87.2%。子集偏不是
    致命的（baseline 与新 run 用同一批题，回归照样看得出来），但**它会让门禁对
    "文档类问题变差"完全失明**——那 78 道题根本没进过 CI。

    所以：先按题型比例分层（DocuMind 的 fact:refuse = 94:30，必拒题不能被截掉），
    再在每层里等距取。（层内序号 = i * 层长 / 取几题，保证跨度铺满整层。）

    取样是确定的（不随机），所以同一个 `--limit` 每次抽到同一批题——baseline 与
    新 run 才可比。
    """
    if only:
        items = [x for x in items if x.get("type") == only]
        if not items:
            sys.exit(f"金标里没有 type={only} 的题")
    if not limit or limit <= 0 or limit >= len(items):
        return items

    if suite.kind == "keyword":
        from .schema import TYPE_REFUSE
        ref = [x for x in items if x.get("type") == TYPE_REFUSE]
        ans = [x for x in items if x.get("type") != TYPE_REFUSE]
        n_ref = max(1, int(limit * len(ref) / len(items))) if ref else 0
        return _even(ref, n_ref) + _even(ans, limit - n_ref)

    return _by_type(items, limit)


def _even(bucket: list[dict], n: int) -> list[dict]:
    """从 bucket 里等距取 n 个，保持原有相对顺序。"""
    if n <= 0:
        return []
    if n >= len(bucket):
        return list(bucket)
    step = len(bucket) / n
    return [bucket[min(int(i * step), len(bucket) - 1)] for i in range(n)]


def _by_type(items: list[dict], limit: int) -> list[dict]:
    """按题型配额等距取：先平均分给每个类型，余数给前面的类型。"""
    buckets: dict[str, list[dict]] = {}
    for it in items:
        buckets.setdefault(it.get("type") or "", []).append(it)
    types = list(buckets)
    base, extra = divmod(limit, len(types))
    picked: list[dict] = []
    for i, t in enumerate(types):
        quota = base + (1 if i < extra else 0)
        picked.extend(_even(buckets[t], quota))
    return picked


# ------------------------------------------------------------------ 真值结果集

def load_gold_results(suite: Suite, items: list[dict], root: Path) -> dict[str, ResultSet] | None:
    """把标准 SQL 执行一次，得到每题的真值结果集。

    只读连接（`mode=ro`）：评测是旁观者，不该有写库的可能。
    金标 SQL 跑不通就直接退出——静默跳过等于伪造分母。
    """
    if suite.kind != "resultset":
        return None
    db = _find_db(suite, root)
    con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    out: dict[str, ResultSet] = {}
    try:
        for it in items:
            if not it["sql"]:
                sys.exit(f"金标 {it['id']} 没有 sql，无法算真值")
            try:
                cur = con.execute(it["sql"])
                cols = [d[0] for d in cur.description or []]
                out[it["id"]] = ResultSet(columns=cols, rows=[list(r) for r in cur.fetchall()])
            except sqlite3.Error as exc:
                sys.exit(f"金标 {it['id']} 执行失败：{exc}\n  SQL: {it['sql'][:200]}")
    finally:
        con.close()
    return out


def _find_db(suite: Suite, root: Path) -> Path:
    for rel in suite.db_candidates:
        cand = root / rel
        if cand.exists():
            return cand
    sys.exit(f"找不到 {suite.name} 的库（找过：{', '.join(suite.db_candidates)}），"
             f"真值结果集算不出来")


# ------------------------------------------------------------------ 请求 / 解析 / 判分

def build_body(suite: Suite, gold_item: dict, opts: dict) -> dict:
    """请求体。两个套件的契约不同，这里只做最小构造。"""
    if suite.kind == "keyword":
        return {"question": gold_item["question"], "mode": opts.get("mode") or "mix"}
    return {"question": gold_item["question"], "session_id": opts.get("session_id") or ""}


def parse(suite: Suite, data: dict) -> dict:
    """把响应摊平成内部字段。

    注意 InsightPilot 的 `status` 是**引擎自评**（ok / clarify / failed / rejected）：
    HTTP 200 但 status=failed 属于「答错了」，不是「请求失败」，所以它不会被记成
    `error`——`error` 只留给连不上 / 超时 / 429 这类传输层问题。
    """
    if suite.kind == "keyword":
        chunks = data.get("chunks") or []
        return {
            "answer": data.get("answer") or "",
            "refused": bool(data.get("refused")),
            "contexts": [c.get("snippet") or "" for c in chunks],
            "citations": data.get("citations") or [],
            "elapsed_ms": data.get("elapsed_ms"),
            "cost_cny": data.get("cost_cny"),
            "tokens": data.get("tokens"),
        }

    a = data.get("answer") or {}
    rep = data.get("report") or {}
    ins = a.get("insight") or {}
    if not isinstance(ins, dict):
        ins = {}
    return {
        # headline 是人读的一句话结论；没有就退回「N 行 × M 列」，让 diff 至少看得出形状
        "answer": ins.get("headline") or a.get("message") or
                  f"{len(a.get('rows') or [])} 行 × {len(a.get('columns') or [])} 列",
        "refused": False,
        "contexts": [],
        "citations": [],
        "elapsed_ms": a.get("elapsed_ms") or rep.get("elapsed_ms") or None,
        "cost_cny": None,
        "tokens": None,
        "columns": list(a.get("columns") or []),
        "rows": [list(r) for r in (a.get("rows") or [])],
        "sql": a.get("sql") or "",
        "status": a.get("status") or rep.get("status") or "",
        "message": a.get("message") or "",
        "model": a.get("model_used") or "",
        "cached": bool(a.get("cached")),
        "attempts": list(rep.get("path") or []).count("executor"),
    }


def judge(suite: Suite, item: Any, gold_item: dict, parsed: dict,
          gold_results: dict[str, ResultSet] | None) -> None:
    """就地填 item 的判分字段。

    InsightPilot 的判分结果同时写进两个地方，各有用处：
      * `kw_hit` = 严格等价（ex）——这样 `passed` / `answerable_rate` /
        `diff` 这些既有能力一行都不用改就能用；
      * `item.judge` 存明细（ex / cover / diff / sql / gold_sql / status / attempts），
        `cover` 与 `ex` 的差值就是「答对但多给了列」。
    """
    if suite.kind == "keyword":
        item.kw_hit = hit(parsed["answer"], gold_item.get("must_include") or [])
        absent = gold_item.get("must_absent") or []
        item.kw_leak = bool(absent) and any(a in parsed["answer"] for a in absent)
        if item.type == "refuse":
            item.refuse_ok = parsed["refused"] is True
        return

    gold = (gold_results or {}).get(gold_item["id"])
    pred = ResultSet(columns=parsed["columns"], rows=parsed["rows"])
    if gold is None:
        # 理论上不可达：load_gold_results 已经把每题都算过
        ex, diff, cover, cover_diff = False, "缺真值结果集", False, "缺真值结果集"
    else:
        ex, diff = compare(gold, pred)
        # 严格判定已通过时，覆盖判定必然通过，不必重复计算。
        cover, cover_diff = (True, "结果一致") if ex else compare_covering(gold, pred)

    item.kw_hit = ex
    # 结果集判据没有"泄漏"这个概念（不该出现的说法是关键词套件的事），
    # 留 None 而不是 False：False 会让 leak_rate 变成一个有意义的 0%。
    item.kw_leak = None
    item.judge = {
        "ex": ex,
        "cover": cover,
        "diff": diff,
        "cover_diff": cover_diff,
        "sql": parsed["sql"],
        "gold_sql": gold_item["sql"],
        "status": parsed["status"],
        "attempts": parsed["attempts"],
        "cached": parsed["cached"],
        "model": parsed["model"],
    }
