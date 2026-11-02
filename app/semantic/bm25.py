"""手写 BM25 —— 不引 rank_bm25 / sklearn / chromadb。

为什么自己写：语义层只有几十张卡片，一次查询要在毫秒级返回，装一个向量库
（带模型权重、带索引文件、带随机种子）反而让"为什么召回这张卡"不可解释。
这里的排序结果完全由 token 统计量决定，可以逐项复算，trace 里写 `bm25:0.83`
时面试官能当场手算验证。

中文没有空格，按 **2-gram** 切：`平均评分 -> 平均 / 均评 / 评分`。
选 2-gram 而不是 jieba 这类分词的代价是"边界敏感"（`多少本书` 切出的 `本书`
和 `书籍数` 切出的 `书籍` 不重合），收益是零依赖、零词典维护；边界问题交给
retriever 的同义词扩展来补（见 `retriever.py` 的 `_expand`）。
"""
from __future__ import annotations

import math
import re
from collections import Counter

# --------------------------------------------------------------------------
# 停用词
# --------------------------------------------------------------------------
# 疑问词必须进表：`多少本书` 若不切词就会因为指标定义里写着"含多少"而误命中，
# 这是语义检索里最典型的假阳性来源。
STOPWORDS: frozenset[str] = frozenset({
    # 疑问 / 祈使 —— 这类词几乎出现在每条问题里，idf 极低但会放大噪声
    "多少", "哪些", "哪个", "哪种", "什么", "怎么", "怎样", "如何", "为什么",
    "请问", "帮我", "麻烦", "查一下", "看一下", "告诉我", "统计",
    # 虚词
    "的", "了", "是", "在", "和", "与", "及", "或", "以及", "或者",
    "有", "对", "把", "被", "给", "都", "也", "就", "还", "会", "能",
    # 指示 / 代词 / 量词
    "这个", "那个", "这些", "那些", "一个", "一些", "一下", "我们", "你们",
    "他们", "可以", "需要", "每个", "所有",
    # 语气
    "吗", "呢", "吧", "啊", "呀",
})

# 单字虚词集合：2-gram 里两个字符都是虚词时整块丢掉（`有的` / `了个`），
# 一个实词一个虚词时保留（`类的` 这种碎片无害，误杀实词才是真损失）。
_STOP_CHARS: frozenset[str] = frozenset("的了是在和与有我对把被给都也就还这那些什么请帮个吗呢吧啊呀")

# 以虚词开头的 2-gram 是跨词边界切出来的碎片（`有多` / `的评` / `个分`）。
# 不丢的话 `有多少人评分` 会切出 `有多` 去命中 `有多少条评论`，idf 还很高
# （只出现在一个文档里），假阳性的分反而压过真命中。
_LEAD_STOP: frozenset[str] = frozenset("的了是在和与把被给这那个些有")

_CJK_RUN = re.compile(r"[\u4e00-\u9fff]+")
_WORD = re.compile(r"[a-zA-Z_][a-zA-Z0-9_]*|\d+(?:\.\d+)?")
# 先抽日期，否则 `2026-10` 会被拆成 2026 / 10，时间窗过滤就失效了。
_DATE = re.compile(r"\d{4}-\d{1,2}(?:-\d{1,2})?")


def _is_cjk(token: str) -> bool:
    return bool(token) and all("\u4e00" <= ch <= "\u9fff" for ch in token)


def tokenize(text: str, *, unigrams: bool = False) -> list[str]:
    """把中英混排的问题切成 BM25 用的 token 列表。

    - 中文连续段按 2-gram 切；单字段落（`书`）原样保留，否则一个字都检索不到。
    - 英文/数字按 `[a-zA-Z_][a-zA-Z0-9_]*|\\d+(\\.\\d+)?` 抽出并转小写。
    - 含下划线的标识符额外补上拆分片段（`fact_rating` -> `fact` / `rating`），
      这样英文提问 "rating trend" 也能命中 `fact_rating`。
    - `unigrams=True` 时补上中文单字，BM25 会给单字更低的权重（见 `BM25._term_weight`）。

    `多少`/`哪些` 等疑问词一律丢弃；这是"多少本书"不会误命中"含多少评分"的关键。
    """
    if not text:
        return []
    raw = text.lower()
    tokens: list[str] = []

    for match in _DATE.finditer(raw):
        tokens.append(match.group())

    for run in _CJK_RUN.findall(raw):
        if len(run) == 1:
            if run not in STOPWORDS:
                tokens.append(run)
            continue
        for i in range(len(run) - 1):
            gram = run[i:i + 2]
            if gram in STOPWORDS:
                continue
            if gram[0] in _LEAD_STOP:
                continue
            if gram[0] in _STOP_CHARS and gram[1] in _STOP_CHARS:
                continue
            tokens.append(gram)
        if unigrams:
            tokens.extend(ch for ch in run if ch not in _STOP_CHARS)

    for word in _WORD.findall(raw):
        if not word.strip("_") or word in STOPWORDS:
            continue
        tokens.append(word)
        if "_" in word:
            tokens.extend(
                part for part in word.split("_")
                if len(part) >= 2 and part not in STOPWORDS
            )
    return tokens


class BM25:
    """标准的 Okapi BM25，idf 用 log1p 形式保证恒正。

    `idf = ln((N - df + 0.5) / (df + 0.5) + 1)`：直接写 ln(ratio) 时，
    当某个词出现在超过一半文档里 ratio < 1，idf 变负，负分会把"命中停用词"
    的文档排到最后而不是忽略，排序直接反了。log1p 形式把下界锁在 0。
    """

    def __init__(self, docs: list[list[str]], k1: float = 1.5,
                 b: float = 0.75, unigram_weight: float = 0.4) -> None:
        self.k1 = k1
        self.b = b
        self.unigram_weight = unigram_weight
        self._docs = [list(doc) for doc in docs]
        self._n = len(self._docs)
        self._tf = [Counter(doc) for doc in self._docs]
        self._dl = [len(doc) for doc in self._docs]
        total = sum(self._dl)
        self._avgdl = total / self._n if self._n else 0.0
        df: Counter[str] = Counter()
        for tf in self._tf:
            df.update(tf.keys())
        self._df = df
        self._idf = {
            term: math.log1p((self._n - freq + 0.5) / (freq + 0.5))
            for term, freq in df.items()
        }

    @property
    def size(self) -> int:
        return self._n

    def idf(self, term: str) -> float:
        """未见过的词用一个"最大稀有度"的 idf；不抛 KeyError，检索路径不该因缺词中断。"""
        if term in self._idf:
            return self._idf[term]
        if not self._n:
            return 0.0
        return math.log1p((self._n + 0.5) / 0.5)

    def _term_weight(self, term: str) -> float:
        # 中文单字歧义大（"书"能命中一切书籍相关的卡），降权保留而不是全丢。
        return self.unigram_weight if len(term) == 1 and _is_cjk(term) else 1.0

    def score(self, query_tokens: list[str]) -> list[float]:
        """对每个文档打分；顺序与构造时的 docs 一一对应。空语料返回空列表。"""
        if not self._n:
            return []
        scores = [0.0] * self._n
        if not query_tokens:
            return scores
        k1, b = self.k1, self.b
        avgdl = self._avgdl or 1.0
        for term in query_tokens:
            idf = self._idf.get(term)
            if idf is None:
                continue
            weight = self._term_weight(term)
            for i, tf in enumerate(self._tf):
                freq = tf.get(term)
                if not freq:
                    continue
                norm = k1 * (1 - b + b * self._dl[i] / avgdl)
                scores[i] += weight * idf * (freq * (k1 + 1)) / (freq + norm)
        return scores
