"""InsightPilot 数据工程：把 Django 业务库转换成面向分析的小型数仓快照。

为什么这样设计：

* 源库是 OLTP 的 Django schema（表名带 app 前缀、日期是 datetime、作者是 M2M），
  分析侧要的是「宽维度 + 窄事实 + 统一日期文本」，所以这里做一次性的形状转换。
* DDL 只认 ``data/schema.sql`` 这一份事实来源，运行期读取并 ``executescript``，
  避免 SQL 在 .py 与 .sql 里各存一份、改了一边忘另一边。
* 每次运行先执行 schema.sql 里的 DROP，因此天然幂等：可以反复跑，
  每次产出的快照都是源库的完整快照，不做增量。
* 源库一律只读打开（``mode=ro``），任何情况下都不会写回源库。
"""

from __future__ import annotations

import argparse
import logging
import re
import sqlite3
import sys
import unicodedata
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Iterable

LOGGER = logging.getLogger("insightpilot.etl")

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SCHEMA = PROJECT_ROOT / "data" / "schema.sql"
DEFAULT_OUTPUT = PROJECT_ROOT / "data" / "warehouse.sqlite"
DEFAULT_SOURCE = Path("D:/biyesheji/book_recommend/db.sqlite3")

# 目标库日期列的合法形态；schema 注释里写的就是 TEXT(YYYY-MM-DD)。
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
# 从 pub_date 这种自由文本里兜底提年份：2012-8 / 1978年6月 / 1982-3-1 都能命中。
YEAR_RE = re.compile(r"(?<!\d)((?:19|20)\d{2})(?!\d)")
# 作者名前缀的朝代/国别标注：(清)曹雪芹 / （汉）司马迁 / [英] 罗琳。
BRACKET_PREFIX_RE = re.compile(r"^\s*(?:[（(][^)）]{1,10}[)）]|\[[^\]]{1,10}\])\s*")

YEAR_MIN, YEAR_MAX = 1900, 2027
PRICE_MIN, PRICE_MAX = 0.0, 2000.0
PAGES_MIN, PAGES_MAX = 0, 5000

WEEKDAYS_CN = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]


# ---------------------------------------------------------------------------
# 清洗小工具：全部是纯函数，方便在测试里单独验证
# ---------------------------------------------------------------------------


def parse_day(value: Any) -> str | None:
    """把任意 datetime 文本截成 YYYY-MM-DD；解析不出来返回 None。

    源库的 created_at 形如 ``2026-10-18 02:04:36.640540``，但也可能混进
    ``2026/10/18`` 或空串。这里要求「前 10 位是合法 ISO 日期」，
    过不了就交给调用方决定丢弃还是置空，绝不猜。
    """
    if not isinstance(value, str):
        return None
    head = value.strip()[:10]
    if not DATE_RE.match(head):
        return None
    try:
        date.fromisoformat(head)
    except ValueError:
        return None
    return head


def clean_author_name(raw: str | None) -> str | None:
    """把 ``(清)曹雪芹 / (清)无名氏`` 这串回落文本压成单个人名。

    只在 M2M 关联缺失时使用：按 ``/`` 取第一段，再剥掉开头的朝代/国别括号。
    """
    if not raw:
        return None
    first = raw.split("/")[0].strip()
    # 有些书会写成「（汉）（南朝宋）司马迁」，前缀括号可能不止一层。
    while True:
        stripped = BRACKET_PREFIX_RE.sub("", first, count=1)
        if stripped == first:
            break
        first = stripped
    return first.strip() or None


def clean_pub_year(pub_year: Any, pub_date: Any) -> tuple[int | None, str]:
    """优先用结构化 pub_year，坏了再从 pub_date 文本里捞年份。

    返回值第二项是处置标签，用于对账：valid / from_pub_date / null。
    """
    try:
        year = int(pub_year) if pub_year is not None else None
    except (TypeError, ValueError):
        year = None
    if year is not None and YEAR_MIN <= year <= YEAR_MAX:
        return year, "valid"

    if isinstance(pub_date, str):
        match = YEAR_RE.search(pub_date)
        if match:
            recovered = int(match.group(1))
            if YEAR_MIN <= recovered <= YEAR_MAX:
                return recovered, "from_pub_date"
    return None, "null"


def clean_price(value: Any) -> tuple[float | None, bool]:
    """剔除 <=0 或 >2000 的离谱价格；返回 (值, 是否被判为脏值)。"""
    if value is None:
        return None, False
    try:
        price = float(value)
    except (TypeError, ValueError):
        return None, True
    if price <= PRICE_MIN or price > PRICE_MAX:
        return None, True
    return price, False


def clean_pages(value: Any) -> tuple[int | None, bool]:
    """页码 <=0 或 >5000 视为录入错误，置 NULL 但不删行。"""
    if value is None:
        return None, False
    try:
        pages = int(value)
    except (TypeError, ValueError):
        return None, True
    if pages <= PAGES_MIN or pages > PAGES_MAX:
        return None, True
    return pages, False


def clean_category(value: Any) -> tuple[str, bool]:
    """分类空值统一成「未分类」，顺手去掉首尾空白。"""
    text = (value or "").strip() if isinstance(value, str) else ""
    if not text:
        return "未分类", True
    return text, False


# ---------------------------------------------------------------------------
# 输出库准备
# ---------------------------------------------------------------------------


def prepare_output(out: Path) -> None:
    """确保输出路径可写；Windows 下删不掉要给出可操作的报错，而不是静默失败。"""
    out.parent.mkdir(parents=True, exist_ok=True)
    if not out.exists():
        return
    try:
        out.unlink()
    except OSError as exc:  # 常见于另一个进程（DB 浏览器 / API 服务）正占用文件
        raise SystemExit(
            f"无法覆盖目标库 {out}：{exc}. "
            f"请关闭占用该文件的程序（如 DB 工具、正在运行的 API 服务）后重试。"
        ) from exc


def apply_schema(conn: sqlite3.Connection, schema_path: Path) -> None:
    """直接执行 schema.sql —— 表、索引、视图、DROP 顺序都只有这一处定义。"""
    ddl = schema_path.read_text(encoding="utf-8")
    conn.executescript(ddl)


# ---------------------------------------------------------------------------
# 各表装载
# ---------------------------------------------------------------------------


def load_dim_author(src: sqlite3.Connection, dst: sqlite3.Connection) -> dict[str, Any]:
    rows = src.execute("SELECT id, name, life FROM books_author ORDER BY id").fetchall()
    dst.executemany(
        "INSERT INTO dim_author(author_id, name, life) VALUES (?, ?, ?)",
        [(r[0], r[1], r[2]) for r in rows],
    )
    return {"source": len(rows), "target": len(rows), "dropped": 0, "reasons": {}}


def load_dim_book(
    src: sqlite3.Connection, dst: sqlite3.Connection
) -> dict[str, Any]:
    """书籍维度是整条链路里清洗规则最密的一张表，规则都写在这里。"""
    # M2M 里可能一书多作者；按 m.id 升序取第一条即「第一作者」。
    first_author: dict[int, tuple[int, str | None]] = {}
    for book_id, author_id, name in src.execute(
        "SELECT m.book_id, m.author_id, a.name "
        "FROM books_author_books m JOIN books_author a ON a.id = m.author_id "
        "ORDER BY m.book_id, m.id"
    ):
        first_author.setdefault(book_id, (author_id, name))

    rows = src.execute(
        "SELECT id, title, author, publisher, pub_year, pub_date, price, category, "
        "       pages, binding, rating_avg, rating_count, douban_rating, douban_votes, "
        "       topic, sentiment_score, positive_rate "
        "FROM books_book ORDER BY id"
    ).fetchall()

    records: list[tuple[Any, ...]] = []
    stats: dict[str, Any] = {
        "source": len(rows),
        "target": 0,
        "dropped": 0,
        "reasons": {},
        "author_from_m2m": 0,
        "author_from_text": 0,
        "pub_year_fixed": 0,
        "pub_year_null": 0,
        "price_nulled": 0,
        "pages_nulled": 0,
        "category_default": 0,
        "rating_count_zero": 0,
    }

    def bump(key: str, n: int = 1) -> None:
        stats[key] += n

    for row in rows:
        (
            book_id,
            title,
            author_raw,
            publisher,
            pub_year_raw,
            pub_date,
            price_raw,
            category_raw,
            pages_raw,
            binding,
            rating_avg,
            rating_count,
            douban_rating,
            douban_votes,
            topic,
            sentiment_score,
            positive_rate,
        ) = row

        # 作者：M2M 优先；关联不到（源库有 2 本书没有任何作者关联）才回落文本列。
        if book_id in first_author:
            author_id, author_name = first_author[book_id]
            bump("author_from_m2m")
        else:
            author_id, author_name = None, clean_author_name(author_raw)
            bump("author_from_text")

        pub_year, year_state = clean_pub_year(pub_year_raw, pub_date)
        if year_state == "from_pub_date":
            bump("pub_year_fixed")
        elif year_state == "null":
            bump("pub_year_null")

        price, price_bad = clean_price(price_raw)
        if price_bad:
            bump("price_nulled")
        pages, pages_bad = clean_pages(pages_raw)
        if pages_bad:
            bump("pages_nulled")

        category, category_default = clean_category(category_raw)
        if category_default:
            bump("category_default")

        # rating_count 是「站内评分人数」，业务上 NULL 等价于 0 人评分。
        if rating_count is None:
            bump("rating_count_zero")
            rating_count = 0

        title_text = (title or "").strip()
        if not title_text:
            # schema 里 title NOT NULL，这里兜个底而不是让整批插入失败。
            title_text = f"未命名-{book_id}"
            bump("title_placeholder")

        records.append(
            (
                book_id,
                title_text,
                author_id,
                author_name,
                publisher,
                pub_year,
                price,
                category,
                pages,
                binding,
                rating_avg,
                rating_count,
                douban_rating,
                douban_votes,
                topic,
                sentiment_score,
                positive_rate,
            )
        )

    dst.executemany(
        "INSERT INTO dim_book(book_id, title, author_id, author_name, publisher, "
        "                     pub_year, price, category, pages, binding, rating_avg, "
        "                     rating_count, douban_rating, douban_votes, topic, "
        "                     sentiment_score, positive_rate) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        records,
    )
    stats["target"] = len(records)
    return stats


def _load_fact(
    src: sqlite3.Connection,
    dst: sqlite3.Connection,
    *,
    select_sql: str,
    insert_sql: str,
    date_fields: dict[int, str],
    required_date_field: str,
    table: str,
) -> dict[str, Any]:
    """事实表通用装载：日期列统一截成 YYYY-MM-DD，关键日期坏掉的行丢弃并计数。

    为什么只丢「关键日期」坏掉的行：事实表的粒度事件都带时间，
    没有日期就没法按天聚合，留着反而会在 dim_date 关联时产生孤儿。
    非关键日期（如 shelf.updated_on）坏掉只置 NULL。
    """
    rows = src.execute(select_sql).fetchall()
    records: list[tuple[Any, ...]] = []
    stats: dict[str, Any] = {"source": len(rows), "target": 0, "dropped": 0, "reasons": {}}

    for row in rows:
        values = list(row)
        ok = True
        for idx, field in date_fields.items():
            parsed = parse_day(values[idx])
            if parsed is None:
                key = f"{field} 无法解析"
                stats["reasons"][key] = stats["reasons"].get(key, 0) + 1
                if field == required_date_field:
                    ok = False
            values[idx] = parsed
        if not ok:
            stats["dropped"] += 1
            continue
        records.append(tuple(values))

    dst.executemany(insert_sql, records)
    stats["target"] = len(records)
    if stats["dropped"]:
        LOGGER.warning(
            "%s: 丢弃 %d 行（源 %d 行），原因 %s",
            table,
            stats["dropped"],
            stats["source"],
            stats["reasons"],
        )
    return stats


def load_fact_rating(src: sqlite3.Connection, dst: sqlite3.Connection) -> dict[str, Any]:
    return _load_fact(
        src,
        dst,
        select_sql=(
            "SELECT id, book_id, user_id, score, created_at, created_at "
            "FROM books_rating ORDER BY id"
        ),
        insert_sql=(
            "INSERT INTO fact_rating(rating_id, book_id, user_id, score, rated_on, rated_at) "
            "VALUES (?,?,?,?,?,?)"
        ),
        date_fields={4: "rated_on"},
        required_date_field="rated_on",
        table="fact_rating",
    )


def load_fact_comment(src: sqlite3.Connection, dst: sqlite3.Connection) -> dict[str, Any]:
    rows = src.execute(
        "SELECT id, book_id, user_id, sentiment, polarity, content, created_at "
        "FROM books_comment ORDER BY id"
    ).fetchall()
    records: list[tuple[Any, ...]] = []
    stats: dict[str, Any] = {"source": len(rows), "target": 0, "dropped": 0, "reasons": {}}

    for comment_id, book_id, user_id, sentiment, polarity, content, created_at in rows:
        created_on = parse_day(created_at)
        if created_on is None:
            stats["reasons"]["created_on 无法解析"] = (
                stats["reasons"].get("created_on 无法解析", 0) + 1
            )
            stats["dropped"] += 1
            continue
        # 源库没有 content_len，分析侧要统计长评/短评，所以在 ETL 里算一次。
        content_len = len(content) if isinstance(content, str) else 0
        records.append(
            (
                comment_id,
                book_id,
                user_id,
                sentiment,
                polarity,
                content_len,
                created_on,
                created_at,
            )
        )

    dst.executemany(
        "INSERT INTO fact_comment(comment_id, book_id, user_id, sentiment, polarity, "
        "                         content_len, created_on, created_at) "
        "VALUES (?,?,?,?,?,?,?,?)",
        records,
    )
    stats["target"] = len(records)
    return stats


def load_fact_click(src: sqlite3.Connection, dst: sqlite3.Connection) -> dict[str, Any]:
    return _load_fact(
        src,
        dst,
        select_sql=(
            "SELECT id, book_id, user_id, event, created_at, created_at "
            "FROM books_clicklog ORDER BY id"
        ),
        insert_sql=(
            "INSERT INTO fact_click(click_id, book_id, user_id, event, clicked_on, clicked_at) "
            "VALUES (?,?,?,?,?,?)"
        ),
        date_fields={4: "clicked_on"},
        required_date_field="clicked_on",
        table="fact_click",
    )


def load_fact_shelf(src: sqlite3.Connection, dst: sqlite3.Connection) -> dict[str, Any]:
    return _load_fact(
        src,
        dst,
        select_sql=(
            "SELECT id, book_id, user_id, status, created_at, updated_at "
            "FROM books_bookshelf ORDER BY id"
        ),
        insert_sql=(
            "INSERT INTO fact_shelf(shelf_id, book_id, user_id, status, created_on, updated_on) "
            "VALUES (?,?,?,?,?,?)"
        ),
        date_fields={4: "created_on", 5: "updated_on"},
        required_date_field="created_on",
        table="fact_shelf",
    )


def load_dim_date(dates: Iterable[str], dst: sqlite3.Connection) -> dict[str, Any]:
    """按事实表覆盖的日期区间逐日生成，保证任何按天聚合都能 LEFT JOIN 上。"""
    unique = sorted({d for d in dates if d})
    if not unique:
        LOGGER.warning("dim_date: 事实表没有任何可用日期，生成 0 行")
        return {"target": 0, "range": None}

    start = date.fromisoformat(unique[0])
    end = date.fromisoformat(unique[-1])
    records: list[tuple[Any, ...]] = []
    cursor = start
    while cursor <= end:
        weekday = cursor.weekday()  # 0=周一，与 Excel 不同，符合 DDL 注释
        records.append(
            (
                cursor.isoformat(),
                cursor.year,
                (cursor.month - 1) // 3 + 1,
                cursor.month,
                cursor.day,
                weekday,
                WEEKDAYS_CN[weekday],
                1 if weekday >= 5 else 0,
            )
        )
        cursor += timedelta(days=1)

    dst.executemany(
        "INSERT INTO dim_date(date_key, year, quarter, month, day, weekday, "
        "                     weekday_cn, is_weekend) VALUES (?,?,?,?,?,?,?,?)",
        records,
    )
    return {"target": len(records), "range": (start.isoformat(), end.isoformat())}


# ---------------------------------------------------------------------------
# 对账输出
# ---------------------------------------------------------------------------


def _pad(text: Any, width: int, *, right: bool = False) -> str:
    """按终端显示宽度补空格 —— 中文占 2 列，len() 对齐会让日志表错位。"""
    text = str(text)
    shown = sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in text)
    fill = " " * max(0, width - shown)
    return fill + text if right else text + fill


def log_reconciliation(stats: dict[str, Any]) -> None:
    """把「源行数 / 目标行数 / 丢弃行数 / 原因」打成一张可肉眼扫的表。"""
    lines = [
        "",
        "对账表（目标 data/warehouse.sqlite）",
        _pad("目标表", 14)
        + _pad("源表", 24)
        + _pad("源行数", 8, right=True)
        + _pad("目标行数", 10, right=True)
        + _pad("丢弃", 6, right=True)
        + "  说明",
        "-" * 96,
    ]
    for table, item in stats["tables"].items():
        notes: list[str] = []
        for key in (
            "author_from_m2m",
            "author_from_text",
            "pub_year_fixed",
            "pub_year_null",
            "price_nulled",
            "pages_nulled",
            "category_default",
            "rating_count_zero",
            "title_placeholder",
        ):
            if item.get(key):
                notes.append(f"{key}={item[key]}")
        for reason, count in item.get("reasons", {}).items():
            notes.append(f"{reason}={count}")
        note = "; ".join(notes)
        source_rows = item.get("source")
        lines.append(
            _pad(table, 14)
            + _pad(item.get("source_name", "-"), 24)
            + _pad(source_rows if source_rows is not None else "-", 8, right=True)
            + _pad(item["target"], 10, right=True)
            + _pad(item.get("dropped", 0), 6, right=True)
            + f"  {note}"
        )
    day = stats.get("dim_date", {})
    if day.get("range"):
        lines.append(f"dim_date 覆盖区间：{day['range'][0]} ~ {day['range'][1]}")
    lines.append("")
    LOGGER.info("\n".join(lines))


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------


def run(
    source: Path = DEFAULT_SOURCE,
    output: Path = DEFAULT_OUTPUT,
    schema_path: Path = DEFAULT_SCHEMA,
) -> dict[str, Any]:
    """执行一次全量转换，返回对账统计（测试直接复用，不解析日志）。"""
    source = Path(source)
    output = Path(output)
    schema_path = Path(schema_path)

    if not source.exists():
        raise SystemExit(f"源库不存在：{source}")
    if not schema_path.exists():
        raise SystemExit(f"schema 文件不存在：{schema_path}")

    prepare_output(output)

    # 源库只读打开：就算代码写错也没有写回去的可能。
    src = sqlite3.connect(f"file:{source.as_posix()}?mode=ro", uri=True)
    dst = sqlite3.connect(str(output))
    try:
        apply_schema(dst, schema_path)

        stats: dict[str, Any] = {"tables": {}}
        stats["tables"]["dim_author"] = {
            **load_dim_author(src, dst),
            "source_name": "books_author",
        }
        stats["tables"]["dim_book"] = {
            **load_dim_book(src, dst),
            "source_name": "books_book",
        }
        stats["tables"]["fact_rating"] = {
            **load_fact_rating(src, dst),
            "source_name": "books_rating",
        }
        stats["tables"]["fact_comment"] = {
            **load_fact_comment(src, dst),
            "source_name": "books_comment",
        }
        stats["tables"]["fact_click"] = {
            **load_fact_click(src, dst),
            "source_name": "books_clicklog",
        }
        stats["tables"]["fact_shelf"] = {
            **load_fact_shelf(src, dst),
            "source_name": "books_bookshelf",
        }

        # dim_date 的区间取所有事实表已落库日期的最小/最大值。
        all_dates: list[str] = []
        for table, column in (
            ("fact_rating", "rated_on"),
            ("fact_comment", "created_on"),
            ("fact_click", "clicked_on"),
            ("fact_shelf", "created_on"),
        ):
            all_dates.extend(
                row[0]
                for row in dst.execute(f"SELECT {column} FROM {table} WHERE {column} IS NOT NULL")
            )
        stats["dim_date"] = load_dim_date(all_dates, dst)
        stats["tables"]["dim_date"] = {
            "source": None,
            "source_name": "由事实表日期推导",
            "target": stats["dim_date"]["target"],
            "dropped": 0,
            "reasons": {},
        }

        dst.commit()

        # 视图/索引由 schema.sql 建好；这里顺手校验一遍，失败要早暴露。
        for name, kind in (
            ("v_book_overview", "view"),
            ("idx_rating_book", "index"),
            ("idx_book_cat", "index"),
        ):
            found = dst.execute(
                "SELECT 1 FROM sqlite_master WHERE name = ? AND type = ?", (name, kind)
            ).fetchone()
            if not found:
                raise RuntimeError(f"schema.sql 未成功创建 {kind}：{name}")

        log_reconciliation(stats)
        LOGGER.info("仓库快照已生成：%s", output)
        return stats
    finally:
        src.close()
        dst.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="源库 -> InsightPilot 分析型仓库快照")
    parser.add_argument(
        "--db",
        dest="db",
        default=str(DEFAULT_OUTPUT),
        help="输出 sqlite 路径（默认 data/warehouse.sqlite）",
    )
    parser.add_argument(
        "--src",
        dest="src",
        default=str(DEFAULT_SOURCE),
        help="源 sqlite 路径（只读，默认 D:/biyesheji/book_recommend/db.sqlite3）",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
    )
    run(Path(args.src), Path(args.db))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
