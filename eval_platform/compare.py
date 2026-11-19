"""评测中台 · 对比与报告

diff:  两个 run 逐题对比，回答「这次改完，到底哪几题变了」
report: 把目录下所有 run 画成一张 HTML（趋势线 + 逐题矩阵），离线单文件，可直接截图

设计约束：
  * 只用标准库，不引 matplotlib / 前端框架，保证在任何机器上能跑
  * 表格与图形都是纯 SVG/HTML 字符串，产物是单个 .html
"""

from __future__ import annotations

import html
from pathlib import Path

from .schema import Run, Item, TYPE_ANSWERABLE, TYPE_REFUSE


# ------------------------------------------------------------------ diff

def align(a: Run, b: Run) -> dict:
    """按题目 id 对齐两次 run。"""
    ia = {i.id: i for i in a.items}
    ib = {i.id: i for i in b.items}
    common = [k for k in ia if k in ib]

    new_ok, new_bad, still_ok, still_bad = [], [], [], []
    for k in sorted(common):
        pa, pb = ia[k].passed, ib[k].passed
        if pa is None or pb is None:
            continue
        if pa and pb:
            still_ok.append(k)
        elif pa and not pb:
            new_bad.append(k)
        elif not pa and pb:
            new_ok.append(k)
        else:
            still_bad.append(k)

    return {
        "only_a": sorted(set(ia) - set(ib)),
        "only_b": sorted(set(ib) - set(ia)),
        "common": len(common),
        "new_ok": new_ok,
        "new_bad": new_bad,
        "still_ok": still_ok,
        "still_bad": still_bad,
        "_ia": ia, "_ib": ib,
    }


def diff_text(a: Run, b: Run, verbose: bool = False) -> str:
    d = align(a, b)
    ma, mb = a.metrics(), b.metrics()
    L = []
    L.append(f"对比  {_label(a)}")
    L.append(f"  →   {_label(b)}")

    # 先比配置再比分数：两次跑的检索实现/预算/去重开着还是关着，
    # 会让同一个模型跑出完全不同的分数。不先看这一行，很容易把
    # "配置变了"误读成"质量变了"。
    cfg = []
    keys = {k for k in list(a.config) + list(b.config)
            # 这三个是"这次跑的时候发生了什么"，不是"这次配置选了什么"：
            # 把它们当配置差异报出来，会把一次网络抖动说成"你改了参数"。
            if k not in ("created_at", "wall_seconds", "n_error", "n_rate_limited")}
    for k in sorted(keys):
        va, vb = a.config.get(k), b.config.get(k)
        if va != vb:
            cfg.append(f"  {k:14s}{va} → {vb}")
    if cfg:
        L.append("")
        L.append("配置不同（先确认这是不是差异的原因）")
        L.extend(cfg)

    L.append("")
    L.append("指标")
    for key, name, unit in [
        ("answerable_rate", "可答率", "pct"),
        ("refuse_accuracy", "拒答准确率", "pct"),
        ("leak_rate", "泄漏率", "pct"),
        ("latency_p50_ms", "p50 延迟", "ms"),
        ("latency_p95_ms", "p95 延迟", "ms"),
        ("cost_per_q_cny", "单题成本", "cny"),
    ]:
        va, vb = ma.get(key), mb.get(key)
        if va is None or vb is None:
            continue
        L.append(f"  {name:12s}{_fmt(va, unit):>12s} → {_fmt(vb, unit):<12s}{_delta(va, vb, unit)}")

    r_a = (ma.get("ragas") or {})
    r_b = (mb.get("ragas") or {})
    if r_a and r_b:
        L.append("  RAGAS")
        for k in ("faithfulness", "answer_relevancy", "context_precision",
                  "context_recall_keyword_proxy"):
            if k in r_a and k in r_b:
                L.append(f"    {k:28s}{r_a[k]:>8.4f} → {r_b[k]:<8.4f}{_delta(r_a[k], r_b[k], 'num')}")

    L.append("")
    L.append(f"逐题（共同 {d['common']} 题）")
    L.append(f"  新对 {len(d['new_ok']):3d}  {_lst(d['new_ok'], verbose)}")
    L.append(f"  新错 {len(d['new_bad']):3d}  {_lst(d['new_bad'], verbose)}")
    L.append(f"  仍对 {len(d['still_ok']):3d}  {_lst(d['still_ok'], verbose)}")
    L.append(f"  仍错 {len(d['still_bad']):3d}  {_lst(d['still_bad'], verbose)}")
    if d["only_b"]:
        L.append(f"  仅新批次有 {len(d['only_b'])}: {_lst(d['only_b'], verbose)}")
    if d["only_a"]:
        L.append(f"  仅旧批次有 {len(d['only_a'])}: {_lst(d['only_a'], verbose)}")

    # 新错的题把两次答案并排给出来，这是 diff 最有用的一段
    if d["new_bad"]:
        L.append("")
        L.append("新错的题（旧答案 → 新答案）")
        for k in d["new_bad"][:10]:
            ia, ib = d["_ia"][k], d["_ib"][k]
            L.append(f"  [{k}] {ia.question[:60]}")
            L.append(f"     旧: {_cut(ia.answer)}")
            L.append(f"     新: {_cut(ib.answer)}")
    return "\n".join(L)


def _label(r: Run) -> str:
    bits = [r.suite]
    if r.tag:
        bits.append(r.tag)
    if r.created_at:
        bits.append(r.created_at.replace("T", " ")[:16])
    bits.append(Path(r.source).name if r.source else "")
    return " ".join(b for b in bits if b)


def _fmt(v, unit) -> str:
    if unit == "pct":
        return f"{v * 100:.2f}%"
    if unit == "ms":
        return f"{int(v):,} ms"
    if unit == "cny":
        return f"¥{v:.6f}"
    return f"{v:.4f}"


def _delta(a, b, unit) -> str:
    d = b - a
    if unit == "pct":
        s = f"{d * 100:+.2f}pp"
    elif unit == "ms":
        s = f"{int(d):+,} ms"
    elif unit == "cny":
        s = f"{d:+.6f}"
    else:
        s = f"{d:+.4f}"
    # 延迟和成本是「越低越好」，用箭头方向表达好坏，不用颜色
    if unit in ("ms", "cny"):
        mark = "↓ 更好" if d < 0 else ("↑ 更差" if d > 0 else "")
    else:
        mark = "↑ 更好" if d > 0 else ("↓ 更差" if d < 0 else "")
    return f"  ({s}) {mark}".rstrip()


def _lst(xs, verbose) -> str:
    if not xs:
        return "-"
    if verbose or len(xs) <= 12:
        return " ".join(xs)
    return " ".join(xs[:12]) + f" …共 {len(xs)} 题"


def _cut(s: str, n: int = 90) -> str:
    s = (s or "").replace("\n", " ")
    return s if len(s) <= n else s[:n] + "…"


# ------------------------------------------------------------------ report

def report_html(
    runs: list[Run],
    title: str = "评测中台 · 回归看板",
    min_n: int = 20,
    max_error: float = 0.2,
) -> str:
    """生成单文件 HTML 看板。

    两条过滤规则，都是为了不让看板说谎：

    min_n      样本量太小的 run（比如只跑了 2 题）打出 100% 会让趋势线出现假尖峰，
               看图的人会以为系统忽上忽下，其实那是"只跑了两题"。
    max_error  失败率超过 max_error 的 run 不进趋势 —— 那个百分比的真实含义是
               "网络挂了"，不是"答得差"，混进来会把质量曲线压成锯齿。
    """
    def usable(r: Run) -> bool:
        if not r.items or len(r.items) < min_n:
            return False
        return sum(1 for i in r.items if i.error) / len(r.items) <= max_error

    shown = [r for r in runs if usable(r)]
    dropped = [r for r in runs if r.items and not usable(r)]
    shown.sort(key=lambda r: r.created_at or "")
    if not shown:
        return ("<html><meta charset='utf-8'><body style=\"font:14px sans-serif;padding:30px\">"
                f"<p>没有样本量 ≥ {min_n} 的 run。用 --min-n 调低门槛。</p></body></html>")

    W, H, PAD_L, PAD_B, PAD_T = 820, 210, 48, 30, 16
    pts_v = [(r.created_at.replace("T", " ")[5:16], (r.metrics().get("answerable_rate") or 0)) for r in shown]
    lo = min(v for _, v in pts_v)
    hi = max(v for _, v in pts_v)
    lo, hi = max(0.0, lo - 0.06), min(1.0, hi + 0.06)
    span = (hi - lo) or 1.0
    n = len(pts_v)

    def px(i: int) -> float:
        return PAD_L + (W - PAD_L - 24) * (i / max(1, n - 1))

    def py(v: float) -> float:
        return H - PAD_B - (H - PAD_B - PAD_T) * ((v - lo) / span)

    poly = " ".join(f"{px(i):.1f},{py(v):.1f}" for i, (_, v) in enumerate(pts_v))
    dots = "".join(
        f'<circle cx="{px(i):.1f}" cy="{py(v):.1f}" r="4" fill="#2457AE"/>'
        f'<text x="{px(i):.1f}" y="{py(v) - 9:.1f}" font-size="10" fill="#2457AE" '
        f'text-anchor="middle">{v * 100:.1f}</text>'
        for i, (_, v) in enumerate(pts_v)
    )
    xlab = "".join(
        f'<text x="{px(i):.1f}" y="{H - PAD_B + 14}" font-size="9" fill="#888" '
        f'text-anchor="end" transform="rotate(-25 {px(i):.1f} {H - PAD_B + 14})">{html.escape(t)}</text>'
        for i, (t, _) in enumerate(pts_v)
    )
    grid = "".join(
        f'<line x1="{PAD_L}" y1="{py(lo + span * f):.1f}" x2="{W - 24}" y2="{py(lo + span * f):.1f}" stroke="#E9EFF8"/>'
        f'<text x="{PAD_L - 7}" y="{py(lo + span * f) + 3:.1f}" font-size="9" fill="#999" text-anchor="end">'
        f'{int((lo + span * f) * 100)}%</text>'
        for f in (0, .25, .5, .75, 1.0)
    )

    # 逐题矩阵：一行一次 run，列是题号。比"行=题"矮得多，也更适合宽屏。
    qids = sorted({i.id for r in shown for i in r.items}, key=_qkey)
    cell = 9
    mx_w = len(qids) * cell

    def strip(r: Run) -> str:
        by = {i.id: i for i in r.items}
        out = []
        for k, qid in enumerate(qids):
            it = by.get(qid)
            if it is None:
                color, tip = "#F2F4F7", "无记录"
            elif it.error:
                color, tip = "#C0392B", f"请求失败：{it.error[:40]}"
            elif it.passed:
                color, tip = "#2E9E6B", "答对"
            elif it.type == TYPE_REFUSE:
                color, tip = "#E67E22", "该拒未拒"
            else:
                color, tip = "#D64545", "答错"
            out.append(f'<rect x="{k * cell}" y="0" width="{cell - 1}" height="11" rx="1.5" '
                       f'fill="{color}"><title>{qid} · {tip}</title></rect>')
        return "".join(out)

    rows = "".join(
        f'<div class="mrow"><span class="mlab">{html.escape((r.tag or "run") + " " + r.created_at[5:16].replace("T", " "))}'
        f'<b>{(r.metrics().get("answerable_rate") or 0) * 100:.1f}%</b></span>'
        f'<svg width="{mx_w}" height="11" style="display:block">{strip(r)}</svg></div>'
        for r in shown
    )
    ticks = "".join(
        f'<text x="{k * cell}" y="9" font-size="8" fill="#bbb">{html.escape(qids[k])}</text>'
        for k in range(0, len(qids), 10)
    )

    cards = "".join(_card(r) for r in shown[-3:][::-1])
    note = ""
    if dropped:
        note = f'（另有 {len(dropped)} 个 run 因样本量 &lt; {min_n} 或失败率 &gt; {int(max_error * 100)}% 未展示）'

    return f"""<!doctype html><html lang="zh"><head><meta charset="utf-8">
<title>{html.escape(title)}</title>
<style>
 body{{font:13px/1.6 -apple-system,"Microsoft YaHei",sans-serif;color:#1a1a1a;margin:0;padding:26px 30px;background:#fff}}
 h1{{font-size:19px;margin:0 0 4px}} .sub{{color:#8a8a8a;font-size:12px;margin-bottom:22px}}
 h2{{font-size:13.5px;margin:26px 0 10px;padding-left:9px;border-left:3px solid #2457AE;font-weight:600}}
 .cards{{display:flex;gap:12px;flex-wrap:wrap}}
 .c{{border:1px solid #E5EAF2;border-radius:8px;padding:11px 15px;min-width:170px}}
 .c .k{{color:#8a8a8a;font-size:11px}} .c .v{{font-size:20px;font-weight:600;color:#2457AE;margin:1px 0}}
 .mrow{{display:flex;align-items:center;gap:10px;margin-bottom:3px}}
 .mlab{{width:150px;flex:none;font-size:10.5px;color:#666;text-align:right;font-family:Consolas,monospace}}
 .mlab b{{color:#2457AE;margin-left:6px}}
 .ticks{{margin-left:160px}}
 .lg{{font-size:11px;color:#8a8a8a;margin-top:9px}}
 .lg i{{display:inline-block;width:9px;height:9px;border-radius:2px;margin:0 4px 0 11px}}
</style></head><body>
<h1>{html.escape(title)}</h1>
<div class="sub">展示 {len(shown)} 次 run{note} · 最近一次 {html.escape(shown[-1].created_at.replace("T", " "))}</div>

<h2>最近三次</h2>
<div class="cards">{cards}</div>

<h2>可答率趋势</h2>
<svg width="{W}" height="{H}">{grid}
 <polyline points="{poly}" fill="none" stroke="#2457AE" stroke-width="2"/>{dots}{xlab}
</svg>

<h2>逐题结果（每行一次 run，每列一题）</h2>
<div class="ticks"><svg width="{mx_w}" height="11">{ticks}</svg></div>
{rows}
<div class="lg"><i style="background:#2E9E6B"></i>答对<i style="background:#D64545"></i>答错
<i style="background:#E67E22"></i>该拒未拒<i style="background:#C0392B"></i>请求失败
<i style="background:#F2F4F7"></i>无记录</div>
</body></html>"""


def _qkey(qid: str) -> tuple:
    """Q001 < Q002 < ... < Q124，数字按数值排而不是字典序。"""
    import re as _re
    m = _re.match(r"^([A-Za-z]*)(\d+)", qid or "")
    return (m.group(1), int(m.group(2))) if m else (qid or "", 0)


def _card(r: Run) -> str:
    m = r.metrics()
    rate = m.get("answerable_rate")
    p50 = m.get("latency_p50_ms")
    cost = m.get("cost_per_q_cny")
    return (
        f'<div class="c"><div class="k">{html.escape(r.tag or "run")} · '
        f'{html.escape(r.created_at[5:16].replace("T", " "))}</div>'
        f'<div class="v">{f"{rate * 100:.1f}%" if rate is not None else "—"}</div>'
        f'<div class="k">可答率 · {m.get("n_answerable_pass")}/{m.get("n_answerable")}</div>'
        f'<div class="k" style="margin-top:7px">p50 {f"{int(p50):,} ms" if p50 else "—"}'
        f' · 单题 {f"¥{cost:.5f}" if cost else "—"}</div></div>'
    )
