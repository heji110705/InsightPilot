"""RAGAS 评分：读一个 run 里已存的 answer / contexts，补打 faithfulness 等分。

为什么要做成独立子命令（而不是塞进 `run`）
------------------------------------------
采集要花钱、评分不花钱但慢：`ragas` + `datasets` 是好几百兆的依赖，而且裁判模型
的说法一改，同一批作答就得重打分。把两件事绑在一起，等于每改一次评分口径都要重跑
一遍线上——既费钱又让"同一批 answer 只能被打一次分"。

口径（与 `_p1/ragas_eval.py` 逐条一致，别改）
---------------------------------------------
1. **拒答题也进分母**。上一版把 refused 直接过滤掉，"拒答是否正确"这件事就完全
   没进统计；现在拒答题即使没有 contexts 也收进样本。
2. **拒答且没有编造 → faithfulness 记 1.0**。"没有编造"本身就是忠实；记 0 会把
   "正确拒答"惩罚成幻觉，方向反了。拒答但出现了 must_absent 的说法 → 0.0。
3. **context_recall 用确定性代理**。RAGAS 原版 ContextRecall 需要参考答案，而
   v2 金标只有 must_include 关键词组，没有参考答案。不自己编参考答案，改判
   "每个关键词组是否至少有一个别名出现在召回上下文里"（组间 AND），单列命名
   `context_recall_keyword_proxy`，不冒充原版指标。
"""

from __future__ import annotations

from typing import Any

from .schema import Item, TYPE_REFUSE

__all__ = ["DEFAULT_MODEL", "JudgeEndpoint", "NoSamples", "RagasUnavailable",
           "score_items", "score_run"]

#: 裁判模型默认 DeepSeek —— 不是因为它好，是因为它**不靠免费额度**。
#: 之前默认 qwen3.7-flash，那把 key 的 1M 额度耗尽后每次评分都 403，
#: 而 ragas 又把 403 吞成 NaN，于是"评分失败"长得像"评分为 0"。
DEFAULT_MODEL = "deepseek-chat"
DEFAULT_BASE = "https://api.deepseek.com/v1"

#: answer_relevancy 不用 RAGAS 原版（它要 embedding：反向生成问题再算余弦）。
#: DeepSeek 没有 embeddings 端点，引 fastembed 又要多 140MB 依赖和一次联网下载。
#: 改成**裁判模型直判**：把问题与答案一起给 LLM，问它答案是否切题（0–1）。
#: 指标名如实标为 `answer_relevancy_llm`，不冒充 RAGAS 原版。


class RagasUnavailable(RuntimeError):
    """ragas / 裁判客户端没装起来。"""


class JudgeEndpoint(RagasUnavailable):
    """依赖装好了，但裁判端点不通（key 错 / base 错 / 模型名不存在）。"""


class NoSamples(RuntimeError):
    """这个 run 里没有可用于 RAGAS 的样本。"""


# ------------------------------------------------------------------ 选样本

def usable_items(items: list[Item], limit: int = 0) -> list[Item]:
    """能进 RAGAS 的题：没报错，且有上下文——或它是必拒题。

    必拒题常常没有 citations（模型没引用），响应里 chunks 就是空的，但它必须
    留在分母里，否则"拒答是否正确"又一次被悄悄排除。它的 faithfulness 不依赖
    ragas 打分，由 `_apply_refuse_rule` 按口径覆盖。
    """
    us = [i for i in items
          if not i.error and (i.contexts or i.type == TYPE_REFUSE)]
    return us[:limit] if limit else us


# ------------------------------------------------------------------ 打分

def score_items(
    items: list[Item],
    *,
    model: str = DEFAULT_MODEL,
    base: str | None = None,
    key: str | None = None,
    limit: int = 0,
    progress: bool = True,
) -> dict:
    """就地给 items 写 `ragas`，返回汇总。样本为空时抛 `NoSamples`。"""
    us = usable_items(items, limit)
    if not us:
        raise NoSamples("没有可用于 RAGAS 的样本（全部报错 / 无上下文，且不是必拒题）")

    rows = [{"id": i.id, "question": i.question, "answer": i.answer,
             "contexts": list(i.contexts)} for i in us]
    scores = _evaluate(rows, model=model, base=base, key=key, progress=progress)
    _guard_judge_failure(scores, len(us))

    n_refused = 0
    for it in us:
        got = scores.get(it.id, {})
        faith = got.get("faithfulness")
        if it.type == TYPE_REFUSE:
            n_refused += 1
            # 口径 2：拒答且无编造记 1.0；拒答却说了不该说的记 0.0。
            faith = 0.0 if it.kw_leak else 1.0
        it.ragas = {
            "faithfulness": _num(faith),
            "answer_relevancy_llm": _num(got.get("answer_relevancy_llm")),
            "context_precision": _num(got.get("context_precision")),
            "context_recall_keyword_proxy": _num(_keyword_recall(it)),
        }

    n_nan = {m: sum(1 for it in us
                    if it.type != TYPE_REFUSE and it.ragas and it.ragas.get(m) is None)
             for m in METRICS}
    return {
        "n_scored": len(us),
        "n_refused_scored": n_refused,
        # 打不上的分单独记一笔：均值的样本量比 n_scored 小的时候要看得见
        "n_nan": {m: c for m, c in n_nan.items() if c},
        "model": model,
        "context_recall_note": "代理口径：must_include 各组是否被召回上下文覆盖；"
                               "v2 金标无参考答案，不做 RAGAS 原版 ContextRecall",
    }


def score_run(run: Any, **kwargs) -> dict:
    """对整份 run 打分，返回汇总（含逐项均值）。"""
    meta = score_items(run.items, **kwargs)
    agg = _mean([i.ragas for i in run.items if i.ragas])
    agg.update({k: v for k, v in meta.items()
                if k in ("n_scored", "n_refused_scored", "n_nan", "model")
                and (k != "n_nan" or v)})
    agg["context_recall_note"] = meta["context_recall_note"]
    return agg


METRICS = ("faithfulness", "answer_relevancy_llm", "context_precision")


def _guard_judge_failure(scores: dict[str, dict], n: int) -> None:
    """裁判模型挂了的时候，ragas 只给 NaN，不抛异常——必须自己判。

    `evaluate(raise_exceptions=False)` 的设计是"一题失败不影响整批"，代价是
    **key 失效 / 端点写错**这种情况也会静默产出 NaN。实测：一把无效 key 跑完 3 题，
    ragas 一个异常都没抛，界面显示 `faithfulness 1.0`（那是拒答题按口径补的 1.0），
    `context_precision 0.0`。这种"看起来正常的分数"比报错危险得多。

    所以：全部指标都是 NaN → 直接失败；部分 NaN → 打一行警告，并在汇总里记
    `n_nan`，让"这道题没打上分"能被看见。
    """
    if not scores:
        raise RagasUnavailable("裁判模型没有返回任何分数")

    valid = {m: sum(1 for s in scores.values()
                    if isinstance(s.get(m), (int, float)) and s[m] == s[m])
             for m in METRICS}
    if not any(valid.values()):
        raise RagasUnavailable(
            f"裁判模型全部调用失败（{n} 题、三个指标全是 NaN）。"
            f"常见原因：key 无效/过期、base_url 写错、模型名不在该端点。"
            f"ragas 不会为此抛异常，所以这里替你抛。"
        )
    short = {m: n - c for m, c in valid.items() if c < n}
    if short:
        detail = "，".join(f"{m} 缺 {k} 题" for m, k in short.items())
        print(f"[warn] 有题没打上分：{detail}（NaN 不进均值，但要当心样本量）")


def _key_from_project_env():
    """没有显式传 key 时，从两个项目的 .env 里借一把。

    为什么要有这条：这把 key 本来就是给 DocuMind / InsightPilot 调模型用的，
    裁判模型也是同一个 DeepSeek 账号。跑评测时还要人手动 export 一遍或者
    每次敲 --ragas-key，等于把「忘记」变成一个必现的失败点。
    """
    from pathlib import Path

    for rel in ("projects/documind/.env", "projects/insightpilot/.env"):
        p = Path(rel)
        if not p.exists():
            continue
        for line in p.read_text(encoding="utf-8", errors="ignore").splitlines():
            if "=" not in line or line.lstrip().startswith("#"):
                continue
            k, v = line.split("=", 1)
            if k.strip() in ("LLM_API_KEY", "IP_LLM_API_KEY", "DEEPSEEK_API_KEY"):
                v = v.strip().strip('"').strip("'")
                if v:
                    return v
    return None


def _evaluate(rows: list[dict], *, model: str, base: str | None,
              key: str | None, progress: bool) -> dict[str, dict]:
    """调 ragas。重依赖全在函数内 import —— 不跑 score 的人不必装它们。"""
    try:
        from ragas import EvaluationDataset, evaluate
        from ragas.dataset_schema import SingleTurnSample
        from ragas.metrics import Faithfulness, LLMContextPrecisionWithoutReference
    except Exception as e:  # noqa: BLE001
        raise RagasUnavailable(
            f"RAGAS 未安装或版本不符：{e}\n"
            f"       pip install -U ragas datasets langchain-openai"
        ) from e

    import os

    import openai

    # ragas 0.4.x 的 llm_factory 走异步路径（instructor + AsyncOpenAI）。传同步
    # OpenAI 客户端时它会另建一个客户端，key/base 全部丢失，表现为 401
    # Invalid API-key（实测两次）。这里直接给 AsyncOpenAI。
    api_key = (key or os.environ.get("DEEPSEEK_API_KEY")
               or os.environ.get("LLM_API_KEY") or os.environ.get("OPENAI_API_KEY")
               or _key_from_project_env())
    api_base = base or os.environ.get("OPENAI_BASE_URL", DEFAULT_BASE)
    if not api_key:
        raise RagasUnavailable(
            "没有裁判模型的 key：传 --ragas-key，或设 DEEPSEEK_API_KEY / LLM_API_KEY")
    client = openai.AsyncOpenAI(api_key=api_key, base_url=api_base)
    llm, _ = _build_judge(model, client, api_key, api_base)
    _preflight(client, model, api_key, api_base)

    samples = [
        SingleTurnSample(user_input=r["question"], response=r["answer"],
                         retrieved_contexts=r["contexts"])
        for r in rows
    ]
    result = evaluate(
        dataset=EvaluationDataset(samples=samples),
        metrics=[Faithfulness(), LLMContextPrecisionWithoutReference()],
        llm=llm,
        raise_exceptions=False,
        show_progress=progress,
    )
    df = result.to_pandas()

    out: dict[str, dict] = {}
    for index, row in enumerate(rows):
        out[row["id"]] = {
            "faithfulness": float(df["faithfulness"].iloc[index]),
            "context_precision": float(df["llm_context_precision_without_reference"].iloc[index]),
        }

    # answer_relevancy 另算：LLM 直判，不走 embedding。
    # 拒答题不打这个分 —— 拒答的正确性由 refuse_ok 管，跟"切不切题"是两回事，
    # 混进去会让这个指标变成"拒答率"的影子。
    import asyncio

    answerable = [r for r in rows if not r.get("refused") and r.get("answer")]
    if answerable:
        scores = asyncio.run(_llm_relevance(client, model, answerable))
        for r, sc in zip(answerable, scores):
            if sc is not None:
                out[r["id"]]["answer_relevancy_llm"] = sc
    return out


async def _llm_relevance(client, model: str, rows: list[dict]) -> list[float | None]:
    """让裁判模型直接判"这段答案是否切题"，返回 0–1。失败返回 None。"""
    import asyncio
    import re

    prompt = (
        "判断【答案】是否切题地回答了【问题】。只看是否正面回应问题所问，"
        "不要因为答案简短、措辞朴素而扣分。\n只输出一个 0 到 1 之间的数字，"
        "不要任何解释。1=完全切题，0=答非所问。\n\n"
        "【问题】{q}\n【答案】{a}"
    )
    sem = asyncio.Semaphore(6)

    async def one(r: dict) -> float | None:
        async with sem:
            try:
                resp = await client.chat.completions.create(
                    model=model,
                    messages=[{"role": "user",
                               "content": prompt.format(q=r["question"], a=r["answer"][:1200])}],
                    max_tokens=8, temperature=0,
                )
                txt = (resp.choices[0].message.content or "").strip()
                m = re.search(r"[01](?:\.\d+)?", txt)
                if not m:
                    return None
                return max(0.0, min(1.0, float(m.group())))
            except Exception:  # noqa: BLE001 — 单题失败不拖垮整批
                return None

    return list(await asyncio.gather(*(one(r) for r in rows)))


def _preflight(client, model: str, api_key: str, api_base: str) -> None:
    """开跑前探一次裁判端点：1-token 的对话。

    为什么必须探：`evaluate(raise_exceptions=False)` 会把 401 吞成 NaN 或 0.0，
    实测一把无效 key 跑完 3 题，ragas 一个异常都没抛，报告里躺着
    `context_precision: 0.0` —— 一个"看起来很正常的低分"。与其让人对着假分数
    排查，不如在花钱之前用最小请求把端点问清楚（成本约 ¥0.0001）。
    向量不探了：answer_relevancy 已改成裁判直判，不再依赖 embedding 端点。
    """
    import asyncio

    async def chat():
        return await client.chat.completions.create(
            model=model, messages=[{"role": "user", "content": "ping"}], max_tokens=1)

    try:
        asyncio.run(chat())
    except Exception as e:  # noqa: BLE001
        raise JudgeEndpoint(
            f"裁判端点连通性检查失败：对话（裁判模型）\n"
            f"      model={model}  base={api_base}  key=…{api_key[-4:]}\n"
            f"      {type(e).__name__}: {e}\n"
            f"      换成有效的 --ragas-key / --ragas-base 再跑。"
        ) from e




def _build_judge(model: str, client, api_key: str, api_base: str):
    """构造 ragas 的 LLM 与 embedding。

    两个坑都是实测踩出来的，写在这里免得下次重踩：
    1. `embedding_factory` 第一个位置参数是 **provider** 不是模型名；传
       "text-embedding-v3" 会报 Unsupported provider。
    2. `embedding_factory` 返回的 OpenAIEmbeddings 没有 `embed_query` /
       `embed_documents`，而 ragas 0.4.x 的 AnswerRelevancy 要的正是这套接口
       （AttributeError）。用 langchain 的 OpenAIEmbeddings 包一层就齐了。
    """
    try:
        from ragas.embeddings import LangchainEmbeddingsWrapper, embedding_factory
        from ragas.llms import llm_factory
    except Exception as e:  # noqa: BLE001
        raise RagasUnavailable(f"构造 RAGAS judge 失败：{e}") from e

    llm = llm_factory(model, client=client)
    return llm, None


def _keyword_recall(item: Item) -> float | None:
    """代理召回：每个 must_include 组是否至少有一个别名出现在上下文里。"""
    groups = item.must or []
    if not groups:
        return None
    blob = "\n".join(item.contexts or [])
    return 1.0 if all(any(a in blob for a in g) for g in groups) else 0.0


def _num(v: Any) -> float | None:
    """NaN → None。JSON 里写 NaN 不是合法 JSON，读回来也容易踩坑。"""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if f != f else f


def _mean(rs: list[dict]) -> dict:
    """按指标名求均值，跳过 None/NaN —— 与 `Run.metrics()` 的 ragas 段同口径。"""
    if not rs:
        return {}
    keys = {k for r in rs for k, v in r.items() if isinstance(v, (int, float))}
    out = {}
    for k in sorted(keys):
        vals = [r[k] for r in rs if isinstance(r.get(k), (int, float)) and r[k] == r[k]]
        if vals:
            out[k] = round(sum(vals) / len(vals), 4)
    out["n_scored"] = len(rs)
    return out
