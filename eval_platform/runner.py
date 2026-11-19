"""评测中台 · 采集

按套件逐题打线上接口，记录作答、耗时、成本、判分，产出统一格式的 Run。

两个套件的差异全部收在 `suites.py` 里，这里只做「请求 → 解析 → 判分」这条流水线：

    documind      POST /v1/ask     判关键词组命中 + 拒答
    insightpilot  POST /api/ask    判结果集等价 + 列覆盖（真值来自本地 sqlite 只读执行）

判分口径与两个仓库里的评分同源（`keyword.hit` / `resultset.compare` 都是独立副本
+ 单测钉住，不是 import）——理由：本目录要能整体搬进任意仓库而不依赖别人的模块
搜索路径，同时判据不能在搬运中悄悄变掉。
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

from . import suites
from .keyword import hit  # noqa: F401  —— 老调用点与测试用的是 runner.hit
from .schema import Item, Run, TYPE_ANSWERABLE, TYPE_REFUSE

#: 兼容旧调用点；新代码请用 `suites.get(name).base_url`
DEFAULT_BASE = suites.DOCUMIND.base_url
DEFAULT_GOLD = "projects/documind/eval_set_v2.json"

#: 429 的重试次数。公开 Demo 有「每 IP 每分钟 3 次」的闸门，撞上了应当等
#: `Retry-After` 再来，而不是把限流记成「答错」——那会让分数无故下降。
MAX_429_RETRIES = 3


def find_gold(explicit: str | None, root: Path) -> Path:
    """兼容旧接口：按 DocuMind 套件找金标。"""
    return suites.load_gold(suites.DOCUMIND, explicit, root)[0]


def stratified(items: list[dict], limit: int) -> list[dict]:
    """兼容旧接口：按 DocuMind 的 fact/refuse 分层取样。"""
    return suites.sample(suites.DOCUMIND, items, limit)


def collect(
    *,
    root: Path,
    suite: str = "documind",
    base_url: str | None = None,
    mode: str = "mix",
    gold: str | None = None,
    limit: int = 0,
    sleep: float | None = None,
    timeout: float = 180.0,
    api_key: str = "",
    tag: str = "",
    only: str = "",
    quiet: bool = False,
) -> Run:
    s = suites.get(suite)
    gold_path, gold_items = suites.load_gold(s, gold, root)
    picked = suites.sample(s, gold_items, limit, only)
    # 真值结果集先算：金标 SQL 跑不通就当场退出，不要白花一轮模型钱。
    gold_results = suites.load_gold_results(s, picked, root)

    base = (base_url or s.base_url).rstrip("/")
    url = base + s.ask_path
    gap = s.default_sleep if sleep is None else sleep
    rows: list[Item] = []
    t_start = time.time()

    for n, gq in enumerate(picked, 1):
        body = suites.build_body(s, gq, {"mode": mode})
        t0 = time.time()
        data, err = _post_json(url, body, timeout=timeout, api_key=api_key)
        parsed = suites.parse(s, data) if not err else _empty_parsed(s)

        it = Item(
            id=gq["id"],
            type=gq.get("type") or TYPE_ANSWERABLE,
            question=gq["question"],
            task=gq.get("task", ""),
            answer=parsed["answer"],
            refused=parsed["refused"],
            contexts=parsed["contexts"],
            citations=parsed["citations"],
            elapsed_ms=parsed["elapsed_ms"],
            wall_ms=int((time.time() - t0) * 1000),
            cost_cny=parsed["cost_cny"],
            tokens=parsed["tokens"],
            error=err,
            must=list(gq.get("must_include") or []),
        )
        # 传输层失败的题不判分：它们不进分母（口径 2），判个 False 只会污染 judge 明细。
        if not err:
            suites.judge(s, it, gq, parsed, gold_results)
        rows.append(it)

        if not quiet:
            print(f"[{n:>3}/{len(picked)}] {it.id} {_mark(it, s)} {it.elapsed_ms or '-'}ms  "
                  f"{gq['question'][:34]}")
        if gap > 0 and n < len(picked):
            time.sleep(gap)

    return Run(
        suite=s.name,
        tag=tag,
        created_at=datetime.now().isoformat(timespec="seconds"),
        config={
            "base_url": base,
            "ask_path": s.ask_path,
            "judge": s.spec,
            "mode": mode if s.kind == "keyword" else "",
            "gold": gold_path.name,
            "limit": limit,
            "only": only,
            "sleep_s": gap,
            "wall_seconds": round(time.time() - t_start, 1),
            "n_error": sum(1 for i in rows if i.error),
            "n_rate_limited": sum(1 for i in rows if (i.error or "").startswith("HTTP 429")),
        },
        items=rows,
    )


def _mark(it: Item, s: suites.Suite) -> str:
    if it.error:
        return "ERR"
    if s.kind == "resultset":
        ex = (it.judge or {}).get("ex")
        cover = (it.judge or {}).get("cover")
        return "√" if ex else ("~" if cover else "×")   # ~ = 覆盖达成但严格不等价
    return "√" if it.passed else ("✗" if it.type == TYPE_REFUSE else "×")


def _empty_parsed(s: suites.Suite) -> dict:
    base = {"answer": "", "refused": False, "contexts": [], "citations": [],
            "elapsed_ms": None, "cost_cny": None, "tokens": None}
    if s.kind == "resultset":
        base.update({"columns": [], "rows": [], "sql": "", "status": "",
                     "message": "", "model": "", "cached": False, "attempts": 0})
    return base


def _post_json(url: str, body: dict, *, timeout: float, api_key: str) -> tuple[dict, str | None]:
    """POST 一个 JSON，返回 ``(响应 dict, 错误串)``。

    429 单独处理：等 `Retry-After` 再来（最多 `MAX_429_RETRIES` 次）。限流不是
    「答错」，把 429 记成错题会让分数凭空掉一截，而且掉得毫无规律。
    """
    payload = json.dumps(body).encode("utf-8")
    for attempt in range(MAX_429_RETRIES + 1):
        req = urllib.request.Request(
            url,
            data=payload,
            headers={
                "Content-Type": "application/json",
                **({"x-api-key": api_key} if api_key else {}),
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8")), None
        except urllib.error.HTTPError as e:
            text = e.read()[:200].decode("utf-8", "ignore")
            if e.code == 429 and attempt < MAX_429_RETRIES:
                wait = _retry_after(e.headers.get("Retry-After"))
                time.sleep(wait)
                continue
            return {}, f"HTTP {e.code}: {text}"
        except Exception as e:  # noqa: BLE001 — 单题失败不中断整批，记进 run
            return {}, f"{type(e).__name__}: {e}"
    return {}, "HTTP 429: 重试次数用尽"


def _retry_after(raw: str | None) -> float:
    try:
        return min(max(float(raw), 1.0), 90.0)
    except (TypeError, ValueError):
        return 20.0


def default_out(root: Path, run: Run, out_dir: str | Path = "eval_runs") -> Path:
    d = Path(out_dir)
    if not d.is_absolute():
        d = root / d
    d.mkdir(parents=True, exist_ok=True)
    stamp = (run.created_at or datetime.now().isoformat()).replace(":", "").replace("-", "")[:15]
    name = f"{run.suite}_{run.tag or 'untagged'}_{stamp}.json".replace(" ", "")
    return d / name
