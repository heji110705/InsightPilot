-- InsightPilot warehouse DDL (SQLite)
-- 由 scripts/etl.py 从 D:/biyesheji/book_recommend/db.sqlite3 转换生成。
-- 设计原则：面向分析的重建模 —— 维度表宽而少，事实表窄而长，日期统一为 TEXT(YYYY-MM-DD)。

PRAGMA journal_mode = OFF;

DROP VIEW  IF EXISTS v_book_overview;
DROP TABLE IF EXISTS fact_shelf;
DROP TABLE IF EXISTS fact_click;
DROP TABLE IF EXISTS fact_comment;
DROP TABLE IF EXISTS fact_rating;
DROP TABLE IF EXISTS dim_date;
DROP TABLE IF EXISTS dim_author;
DROP TABLE IF EXISTS dim_book;

CREATE TABLE dim_book (
    book_id        INTEGER PRIMARY KEY,
    title          TEXT    NOT NULL,
    author_id      INTEGER,
    author_name    TEXT,
    publisher      TEXT,
    pub_year       INTEGER,
    price          REAL,
    category       TEXT,
    pages          INTEGER,
    binding        TEXT,
    rating_avg     REAL,     -- 站内平均评分 0-10，仅本站用户打分
    rating_count   INTEGER,  -- 站内评分人数
    douban_rating  REAL,     -- 豆瓣官方评分，与本表 rating_avg 不同源
    douban_votes   INTEGER,  -- 豆瓣评价人数
    topic          INTEGER,  -- LDA 主题编号 0-11
    sentiment_score REAL,    -- 评论情感均值 -1..1
    positive_rate  REAL     -- 正向评论占比 0..1
);

CREATE TABLE dim_author (
    author_id INTEGER PRIMARY KEY,
    name      TEXT,
    life      TEXT
);

CREATE TABLE dim_date (
    date_key   TEXT PRIMARY KEY,   -- YYYY-MM-DD
    year       INTEGER,
    quarter    INTEGER,
    month      INTEGER,
    day        INTEGER,
    weekday    INTEGER,            -- 0=周一
    weekday_cn TEXT,
    is_weekend INTEGER             -- 1/0
);

CREATE TABLE fact_rating (
    rating_id INTEGER PRIMARY KEY,
    book_id   INTEGER NOT NULL,
    user_id   INTEGER NOT NULL,
    score     REAL,                -- 1-5 星
    rated_on  TEXT,                -- YYYY-MM-DD
    rated_at  TEXT
);

CREATE TABLE fact_comment (
    comment_id  INTEGER PRIMARY KEY,
    book_id     INTEGER NOT NULL,
    user_id     INTEGER NOT NULL,
    sentiment   REAL,              -- -1..1
    polarity    TEXT,              -- pos / neu / neg
    content_len INTEGER,
    created_on  TEXT,
    created_at  TEXT
);

CREATE TABLE fact_click (
    click_id   INTEGER PRIMARY KEY,
    book_id    INTEGER NOT NULL,
    user_id    INTEGER NOT NULL,
    event      TEXT,               -- click / view / search ...
    clicked_on TEXT,
    clicked_at TEXT
);

CREATE TABLE fact_shelf (
    shelf_id   INTEGER PRIMARY KEY,
    book_id    INTEGER NOT NULL,
    user_id    INTEGER NOT NULL,
    status     TEXT,               -- wish / reading / read
    created_on TEXT,
    updated_on TEXT
);

CREATE INDEX idx_rating_book ON fact_rating(book_id);
CREATE INDEX idx_rating_user ON fact_rating(user_id);
CREATE INDEX idx_rating_on   ON fact_rating(rated_on);
CREATE INDEX idx_comment_book ON fact_comment(book_id);
CREATE INDEX idx_comment_on   ON fact_comment(created_on);
CREATE INDEX idx_click_book   ON fact_click(book_id);
CREATE INDEX idx_click_on     ON fact_click(clicked_on);
CREATE INDEX idx_shelf_book   ON fact_shelf(book_id);
CREATE INDEX idx_book_cat     ON dim_book(category);
CREATE INDEX idx_book_pub     ON dim_book(publisher);

-- 宽表视图：把「书的评分表现」一次拼好，减少多表 JOIN 的歧义。
--
-- 注意：四张事实表必须**先各自按 book_id 预聚合再 JOIN**。
-- 直接 LEFT JOIN 后 COUNT 会产生笛卡尔放大（rating×comment×click×shelf），
-- 行数看起来仍是对的 250 行，但每个计数都变成四者乘积——这种"测试全绿、
-- 数字全错"的错误比直接报错危险得多，所以这里显式预聚合。
CREATE VIEW v_book_overview AS
SELECT
    b.book_id,
    b.title,
    b.author_name,
    b.publisher,
    b.pub_year,
    b.category,
    b.price,
    b.pages,
    b.rating_avg,
    b.rating_count,
    b.douban_rating,
    b.douban_votes,
    COALESCE(r.rating_cnt, 0)                AS rating_cnt,
    r.rating_score_avg                       AS rating_score_avg,
    COALESCE(c.comment_cnt, 0)               AS comment_cnt,
    c.sentiment_avg                          AS sentiment_avg,
    COALESCE(k.click_cnt, 0)                 AS click_cnt,
    COALESCE(s.shelf_cnt, 0)                 AS shelf_cnt
FROM dim_book b
LEFT JOIN (
    SELECT book_id, COUNT(*) AS rating_cnt, ROUND(AVG(score), 3) AS rating_score_avg
    FROM fact_rating GROUP BY book_id
) r ON r.book_id = b.book_id
LEFT JOIN (
    SELECT book_id, COUNT(*) AS comment_cnt, ROUND(AVG(sentiment), 3) AS sentiment_avg
    FROM fact_comment GROUP BY book_id
) c ON c.book_id = b.book_id
LEFT JOIN (
    SELECT book_id, COUNT(*) AS click_cnt FROM fact_click GROUP BY book_id
) k ON k.book_id = b.book_id
LEFT JOIN (
    SELECT book_id, COUNT(*) AS shelf_cnt FROM fact_shelf GROUP BY book_id
) s ON s.book_id = b.book_id;
