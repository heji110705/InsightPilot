"""BM25 与分词的单元测试 —— 只测纯函数，不碰 YAML。"""

from __future__ import annotations

from app.semantic.bm25 import BM25, tokenize


# --------------------------------------------------------------------------
# 分词
# --------------------------------------------------------------------------
def test_tokenize_chinese_bigram():
    """中文按 2-gram 切，滑窗相邻字符各成一片。"""
    assert tokenize("平均评分") == ["平均", "均评", "评分"]


def test_tokenize_mixed_lowercase():
    """中英混排：英文转小写，带下划线的标识符额外补拆分片段。"""
    tokens = tokenize("Fact_Rating 的 Score 是多少")
    assert "fact_rating" in tokens
    assert "rating" in tokens          # 下划线拆分，英文提问也能命中表名
    assert "score" in tokens
    assert "多少" not in tokens


def test_tokenize_drops_question_words():
    """疑问词必须被丢掉，否则"多少本书"会命中指标定义里的"多少"。"""
    tokens = tokenize("一共有多少本书")
    assert "多少" not in tokens
    assert "本书" in tokens            # 实词碎片保留
    assert "哪些" not in tokenize("哪些分类的评分最高")


def test_tokenize_keeps_date_and_number():
    """日期整体保留：拆成 2026 / 10 会让时间窗过滤失真。"""
    tokens = tokenize("2026-10-01 之后评分 8.5 的书")
    assert "2026-10-01" in tokens
    assert "8.5" in tokens


def test_tokenize_drops_cross_boundary_fragments():
    """跨词边界的虚词碎片（有多 / 的评）不该进入索引。"""
    assert "有多" not in tokenize("有多少条评论")
    assert "的评" not in tokenize("每个分类的平均评分")


def test_tokenize_empty():
    assert tokenize("") == []
    assert tokenize("   ") == []


def test_tokenize_single_char_survives():
    """只有一个汉字的输入不能被吞掉，否则一个字都检索不到。"""
    assert tokenize("书") == ["书"]


# --------------------------------------------------------------------------
# BM25
# --------------------------------------------------------------------------
def test_bm25_empty_corpus_does_not_crash():
    index = BM25([])
    assert index.size == 0
    assert index.score(["任意"]) == []


def test_bm25_empty_query_returns_zeros():
    index = BM25([tokenize("书籍数"), tokenize("平均评分")])
    assert index.score([]) == [0.0, 0.0]


def test_bm25_idf_is_always_positive():
    """log1p 形式：词出现在全部文档里时 idf 也不为负。"""
    docs = [tokenize("评分")] * 5
    index = BM25(docs)
    assert index.idf("评分") > 0
    assert index.idf("从未出现过的词") > 0


def test_bm25_ranks_relevant_document_first():
    docs = [
        tokenize("书籍数 有多少本书 dim_book 的行数"),
        tokenize("平均评分 站内打分均值"),
        tokenize("评论数 一行一条留言"),
    ]
    index = BM25(docs)
    scores = index.score(tokenize("平均评分是多少"))
    assert scores[1] == max(scores)
    assert scores[1] > scores[0]


def test_bm25_is_deterministic():
    docs = [tokenize("书籍数"), tokenize("平均评分"), tokenize("评论数")]
    index = BM25(docs)
    query = tokenize("评分最高的书")
    assert index.score(query) == index.score(query)


def test_bm25_unigram_is_downweighted():
    """中文单字歧义大，权重低于 2-gram（默认 0.4）。"""
    index = BM25([tokenize("书")])
    assert index._term_weight("书") == 0.4
    assert index._term_weight("书籍") == 1.0
