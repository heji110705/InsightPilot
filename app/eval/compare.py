"""结果集等价判定：不比较 SQL 字符串，只比较结果。

判定口径（与评测方案一致）
--------------------------
1. 列数必须相同，**列名不比较**——``COUNT(*) AS 书籍数`` 与 ``AS n`` 等价；
2. 行序不比较：每行元组化后按多重集（multiset）比较，重复行要一一对上；
3. 数值用相对容差 ``abs(a-b) <= max(1e-6, 1e-6*abs(a))``；布尔视为数值
   （``True`` 与 ``1`` 等价），整型 / 浮点混用同理。实现里尺度取 ``max(|a|,|b|)``，
   让容差左右对称：100 与 100.0001 的差刚好是 1e-4，用单侧尺度会被浮点表示
   误差挤到容差外，凭空判错；对称尺度只多出 1e-6 的相对量，不影响 1e-3 的拒绝；
4. 字符串去掉首尾空白后**大小写敏感**比较（数据是中文，不能 lower）；
   ``None`` 与空串是两种不同的值，不相等；
5. 行数不同直接判不等（多一行少一行都不是同一个结果）。

为什么不排序后逐行比
--------------------
排序需要给混合类型（数字 / 字符串 / NULL）定一个全序键，而容差会破坏排序的
稳定性：两行只差 1e-7 时，排序键可能把它们排到不同位置，逐行比较就会误判。
这里先用哈希多重集走"完全相等"的快路径，剩下的行走带容差的一对一匹配；
只有需要给人类看差异时，才排序后找第一处不同。

单值结果（1×1）是这套规则的直接推论：列数 1 对 1、行数 1 对 1、单元格数值
在容差内即为真，别名完全不影响。
"""
from __future__ import annotations

import math
from collections import Counter
from typing import Any, Iterable

from app.core.models import ResultSet

__all__ = ["compare", "compare_covering"]

TOL = 1e-6
MAX_MESSAGE = 120
_ELLIPSIS = "…"


# --------------------------------------------------------------------------
# 单元格
# --------------------------------------------------------------------------
def _is_number(value: Any) -> bool:
    """bool 是 int 的子类，按需求"布尔视为数值"，因此这里不排除 bool。"""
    return isinstance(value, (int, float)) and not isinstance(value, complex)


def _cell_equal(a: Any, b: Any) -> bool:
    if a is None or b is None:
        # None 只与 None 相等；None vs "" 不等。
        return a is None and b is None

    if _is_number(a) and _is_number(b):
        fa, fb = float(a), float(b)
        if math.isnan(fa) or math.isnan(fb):
            return math.isnan(fa) and math.isnan(fb)
        if math.isinf(fa) or math.isinf(fb):
            return fa == fb
        # 尺度取两者绝对值较大者：容差因此对称，且 100 与 100.0001 这种
        # 「差值刚好等于 1e-4」的边界不会被浮点表示误差挤到容差外。
        scale = max(abs(fa), abs(fb))
        return abs(fa - fb) <= max(TOL, TOL * scale)

    if isinstance(a, str) and isinstance(b, str):
        return a.strip() == b.strip()

    if isinstance(a, (bytes, bytearray)) and isinstance(b, (bytes, bytearray)):
        return bytes(a) == bytes(b)

    # 日期对象等其它类型：退化为严格相等（不做字符串化，避免 1 与 "1" 混同）。
    return type(a) is type(b) and a == b


def _canonical(value: Any) -> tuple[str, Any]:
    """哈希快路径的键。所有数值统一成 float，保证 1 == 1.0 == True 的键一致。"""
    if value is None:
        return ("null", None)
    if _is_number(value):
        return ("num", float(value))
    if isinstance(value, str):
        return ("str", value.strip())
    if isinstance(value, (bytes, bytearray)):
        return ("bytes", bytes(value))
    return ("other", repr(value))


def _row_key(row: Iterable[Any]) -> tuple[tuple[str, Any], ...]:
    return tuple(_canonical(v) for v in row)


def _row_equal(gold_row: Iterable[Any], pred_row: Iterable[Any]) -> bool:
    g, p = tuple(gold_row), tuple(pred_row)
    if len(g) != len(p):
        return False
    return all(_cell_equal(a, b) for a, b in zip(g, p))


# --------------------------------------------------------------------------
# 差异说明
# --------------------------------------------------------------------------
def _fmt(value: Any, limit: int = 24) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, str):
        text = value if value != "" else "空串"
        return text if len(text) <= limit else text[:limit] + _ELLIPSIS
    if isinstance(value, bool):
        return "True" if value else "False"
    if isinstance(value, float):
        if math.isfinite(value) and value.is_integer() and abs(value) < 1e15:
            return str(int(value))
        return repr(value)
    text = str(value)
    return text if len(text) <= limit else text[:limit] + _ELLIPSIS


def _clip(message: str) -> str:
    return message if len(message) <= MAX_MESSAGE else message[: MAX_MESSAGE - 1] + _ELLIPSIS


def _cell_diff_message(row_index: int, col_index: int, gold: Any, pred: Any) -> str:
    return _clip(
        f"第 {row_index + 1} 行第 {col_index + 1} 列：{_fmt(gold)} vs {_fmt(pred)}"
    )


# --------------------------------------------------------------------------
# 主入口
# --------------------------------------------------------------------------
def compare(gold: ResultSet, pred: ResultSet) -> tuple[bool, str]:
    """比较两个结果集是否等价。

    返回 ``(等价, 说明)``。说明永远是人可读的一行，最长 120 字；等价时为
    ``"结果一致"``（或注明命中的是哪种宽松规则）。
    """
    gold_cols = list(getattr(gold, "columns", []) or [])
    pred_cols = list(getattr(pred, "columns", []) or [])
    gold_rows = [tuple(row) for row in (getattr(gold, "rows", []) or [])]
    pred_rows = [tuple(row) for row in (getattr(pred, "rows", []) or [])]

    if len(gold_cols) != len(pred_cols):
        return False, _clip(f"列数不同：{len(gold_cols)} vs {len(pred_cols)}")

    if len(gold_rows) != len(pred_rows):
        return False, _clip(f"行数不同：{len(gold_rows)} vs {len(pred_rows)}")

    # 快路径：完全相等（忽略行序）的多重集。绝大多数"对"的题走这里。
    if Counter(map(_row_key, gold_rows)) == Counter(map(_row_key, pred_rows)):
        return True, "结果一致"

    # 容差路径：一对一配对，重复行按多重集消耗。
    if gold_rows:
        used = [False] * len(pred_rows)
        for gold_row in gold_rows:
            match = -1
            for index, pred_row in enumerate(pred_rows):
                if not used[index] and _row_equal(gold_row, pred_row):
                    match = index
                    break
            if match < 0:
                return False, _diff_detail(gold_rows, pred_rows)
            used[match] = True
        return True, "结果一致（数值在容差内）"

    return True, "结果一致"


def _diff_detail(gold_rows: list[tuple], pred_rows: list[tuple]) -> str:
    """找一条"最像"的预测行，报告第一处不同的单元格。

    为什么不直接排序后按位比：排序键一换，`第 1 行` 可能拿金标准的
    「分类=传记纪实」去对预测的「平均定价=45.96」，报出
    「45.96 vs 传记纪实」这种看起来像类型错误的假象。这里先按完全相同
    配对，再为第一条没配上的金标准行挑**相同单元格最多**的预测行，
    差异说明就能落在真正不同的那一列上。
    """
    used = [False] * len(pred_rows)
    gold_paired = [False] * len(gold_rows)

    for gi, grow in enumerate(gold_rows):
        for pi, prow in enumerate(pred_rows):
            if used[pi] or len(grow) != len(prow):
                continue
            if all(_cell_equal(a, b) for a, b in zip(grow, prow)):
                used[pi] = True
                gold_paired[gi] = True
                break

    for gi, grow in enumerate(gold_rows):
        if gold_paired[gi]:
            continue
        best_index = -1
        best_score = -1
        for pi, prow in enumerate(pred_rows):
            if len(prow) != len(grow):
                continue
            score = sum(1 for a, b in zip(grow, prow) if _cell_equal(a, b))
            # 同分时优先未配对的行：它更能解释"这一行为什么对不上"。
            rank = (score, not used[pi])
            if best_index < 0 or rank > (best_score, not used[best_index]):
                best_index, best_score = pi, score
        if best_index < 0:
            continue
        for col_index, (gval, pval) in enumerate(zip(grow, pred_rows[best_index])):
            if not _cell_equal(gval, pval):
                return _cell_diff_message(gi, col_index, gval, pval)

    # 理论上不可达：行数相同且没有单元格差异，就应该在快路径判等。
    return "结果不一致（行内容无法一一配对）"


# --------------------------------------------------------------------------
# 列覆盖判定（附加列容忍）
# --------------------------------------------------------------------------
def _column_multiset(rows: list[tuple], index: int) -> Counter:
    return Counter(_canonical(row[index]) for row in rows)


def _project(rows: list[tuple], mapping: tuple[int, ...]) -> ResultSet:
    # 列名只为满足 compare 的「列数相等」前置检查，判定本身不比较列名。
    return ResultSet(
        columns=[f"col{i}" for i in range(len(mapping))],
        rows=[tuple(row[k] for k in mapping) for row in rows],
        rowcount=len(rows),
    )


def compare_covering(gold: ResultSet, pred: ResultSet) -> tuple[bool, str]:
    """列覆盖判定：**严格判定之外的第二个指标**，不改变 ``compare`` 的语义。

    严格判定要求列数完全相等，这会把「答对了但多展示了几列」也算错。真实问数
    场景里多一列往往不是错误（问「好评率」时顺手给出分子分母，分析师反而更爱看），
    但也不能一概放过——多给 15 列（近似 ``SELECT *``）显然是投影没收住。
    所以这里不做取舍，而是**同时报两个数**：``ex`` 严格、``cover`` 容忍附加列。
    两者之差就是这个系统「答对但投影没收住」的比例，正好是要优化的对象。

    判定规则
    --------
    1. 行数必须相等（多一行少一行都不算覆盖）；
    2. 预测列数不少于金标准列数；
    3. 金标准的每一列都能在预测里找到**一列与之逐行取值相同**的对应列
       （列名不参与，列映射由值的多重集确定，允许任意排列）；
    4. 映射确定后，把预测投影到金标准的列顺序上，再走一遍 ``compare``，
       确保「列对上了、行也没错位」。

    第 3、4 步分开做是有意的：只比列值的多重集，会漏掉「两列各自都对、
    但行与行错位」的情形，投影重比才能抓住它。

    说明文字会区分「多出 N 列」与「列数够但取值不对」，便于归因。
    """
    gold_cols = list(getattr(gold, "columns", []) or [])
    pred_cols = list(getattr(pred, "columns", []) or [])
    gold_rows = [tuple(row) for row in (getattr(gold, "rows", []) or [])]
    pred_rows = [tuple(row) for row in (getattr(pred, "rows", []) or [])]

    if not gold_cols or not pred_cols:
        return False, "结果为空，无法做覆盖判定"

    if len(gold_rows) != len(pred_rows):
        return False, _clip(f"行数不同：{len(gold_rows)} vs {len(pred_rows)}")

    if len(pred_cols) < len(gold_cols):
        return False, _clip(f"列数不足：{len(gold_cols)} vs {len(pred_cols)}")

    gold_sets = [_column_multiset(gold_rows, j) for j in range(len(gold_cols))]
    pred_sets = [_column_multiset(pred_rows, k) for k in range(len(pred_cols))]

    candidates = [
        [k for k in range(len(pred_cols)) if pred_sets[k] == gold_sets[j]]
        for j in range(len(gold_cols))
    ]
    if any(not options for options in candidates):
        return False, "金标准有列在预测里找不到同值列"

    # 候选少的列先定，减少回溯分支。
    order = sorted(range(len(gold_cols)), key=lambda j: len(candidates[j]))
    mapping: list[int] = [-1] * len(gold_cols)

    def search(depth: int, used: set[int]) -> tuple[int, ...] | None:
        if depth == len(order):
            return tuple(mapping)
        j = order[depth]
        for k in candidates[j]:
            if k in used:
                continue
            mapping[j] = k
            found = search(depth + 1, used | {k})
            if found is not None:
                return found
            mapping[j] = -1
        return None

    resolved = search(0, set())
    if resolved is None:
        return False, "金标准各列无法同时映射到预测的不同列"

    projected = _project(pred_rows, resolved)
    ok, detail = compare(
        ResultSet(columns=[""] * len(gold_cols), rows=[list(r) for r in gold_rows],
                  rowcount=len(gold_rows)),
        projected,
    )
    if not ok:
        return False, _clip(f"列能对上但行内容不同：{detail}")

    extra = len(pred_cols) - len(gold_cols)
    if extra == 0:
        return True, "结果一致"
    extra_names = [pred_cols[k] for k in range(len(pred_cols)) if k not in set(resolved)]
    shown = "、".join(extra_names[:3]) + ("…" if len(extra_names) > 3 else "")
    return True, _clip(f"覆盖一致（多出 {extra} 列：{shown}）")
