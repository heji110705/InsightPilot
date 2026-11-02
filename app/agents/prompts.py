"""Prompt construction.

Kept in one place so the SQL prompt, the intent prompt and the insight prompt
can be diffed against each other. Everything the model is allowed to know comes
from the retrieved context — the schema is never dumped wholesale, because a
full schema dump is exactly what makes text-to-SQL drift.
"""
from __future__ import annotations

from app.core.models import SQLContext, TurnState

SYSTEM_INTENT = """你是一个数据分析助手的意图解析器。判断用户问题并输出 JSON。

输出字段：
- kind: "query"（要查数据）、"chat"（寒暄）、"out_of_scope"（与数据无关）
- rewritten: 消解指代后的完整问题。若上一轮有对话，把"再按出版社拆一下"这类补全成完整问句
- entities: 从问题抽出的结构化信息，如 {"limit": 10, "category": "文学小说", "time": "2026-10", "order": "DESC"}
- ambiguities: 含糊词列表，每项 {"term": "最受欢迎", "options": ["点击量最高","收藏数最多"], "chosen": "点击量最高", "reason": "..."}
- defaults_applied: 你替用户做了哪些默认约定，用短句
- needs_clarify: 是否必须回问用户。只有当不同理解会导致查完全不同的表、且无法用默认值兜住时才为 true
- clarify_question: needs_clarify 为 true 时的问题

规则：
1. 口径含糊时**优先自己定默认口径并写进 defaults_applied**，不要动辄回问。例如"最受欢迎"默认取点击量最高，并在 ambiguities 里说明。
2. 只有"帮我看看数据"这种既无指标又无维度的才 needs_clarify。
3. 只输出 JSON，不要任何解释文字。"""

SYSTEM_SQL = """你是 SQLite SQL 工程师。根据给定的表结构与口径说明，把用户问题翻译成一条 SELECT。

硬性规则：
1. 只输出**一条** SELECT 语句，不要 INSERT/UPDATE/DELETE/DDL，不要多条语句。
2. 只能使用「可用表」里出现的表和字段。不要臆造字段名。
3. 表全是 SQLite 表，日期字段是 TEXT（'YYYY-MM-DD'），比较时直接用字符串。
4. 必须带 LIMIT（明细类不超过 50，聚合类可不加）。
5. 每个输出列都要有中文别名（AS 别名），方便非技术用户读。
6. 数值列用 ROUND(x, 2)。比率用 100.0 * 分子 / 分母，并注意分母为 0。
7. 多表关联时注意 1:N 放大：先按主键聚合成子查询再 JOIN，不要直接 JOIN 后 COUNT。
8. 口径陷阱要看「指标口径」里的 caveats：例如评分是 0-10 分不是 1-5 星，点击表里 event 有 expose 和 click 两种。
9. **投影纪律**：只输出与问题相关的列，判断标准是「删掉这一列，用户还能不能核对答案」。
   - 为了核对而附带的分子 / 分母是**允许**的：问"好评率"时同时给评分总数与好评数，
     问"占比"时同时给分子分母——这类列不算多余。
   - 与答案无关的字段**不要带**：问"最贵的书是哪本"就只给书名、定价和判断依据，
     不要附作者、出版社、分类、豆瓣评分，那会把答案变成一张报表。
   - 仅用于 ORDER BY 或 HAVING 的列不必出现在结果里。
10. 「输出约定」段落里的行数、小数位、最小样本量是硬性的，与你的直觉冲突时以它为准。

输出 JSON：{"sql": "...", "rationale": "一句话说明口径选择", "used_tables": ["dim_book"]}
只输出 JSON，不要解释文字。"""

SYSTEM_INSIGHT = """你是数据分析师。根据查询结果写一句结论和最多 3 条要点。

铁律：**只能使用给出的数字**，一个都不能编、不能推算新数字、不能四舍五入成别的数。
可以引用结果里的数字原样，也可以描述排序关系。

输出 JSON：{"headline": "一句话结论", "bullets": ["要点1", "要点2"]}
只输出 JSON。"""


def render_context(context: SQLContext, *, max_columns: int = 24) -> str:
    lines: list[str] = ["## 可用表"]
    for card in context.tables:
        lines.append(f"### {card.name}（{card.grain}）")
        if card.meaning:
            lines.append(card.meaning)
        cols = card.columns[:max_columns]
        lines.append("字段：" + "；".join(cols))
        if len(card.columns) > max_columns:
            lines.append(f"（其余 {len(card.columns) - max_columns} 个字段已省略）")
        for caveat in card.caveats[:4]:
            lines.append(f"- 注意：{caveat}")
    if context.metrics:
        lines.append("\n## 指标口径")
        for metric in context.metrics[:6]:
            lines.append(
                f"### {metric.name}（{metric.key}）"
                + (f" 单位：{metric.unit}" if metric.unit else "")
            )
            if metric.definition:
                lines.append(f"定义：{metric.definition}")
            if metric.formula:
                lines.append(f"公式：{metric.formula}")
            for caveat in metric.caveats[:3]:
                lines.append(f"- 注意：{caveat}")
    if context.examples:
        lines.append("\n## 范例")
        for example in context.examples[:4]:
            lines.append(f"问：{example.question}\nSQL：{example.sql}")
            if example.note:
                lines.append(f"（{example.note}）")
    if context.join_hints:
        lines.append("\n## 关联条件")
        lines.extend(f"- {hint}" for hint in context.join_hints[:8])
    lines.extend(_render_defaults(context.defaults))
    return "\n".join(lines)


def _render_defaults(defaults: dict) -> list[str]:
    """把输出约定渲染成提示词段落。

    这些规则本来散落在 SYSTEM_SQL 里以「默认」「注意」这类软措辞出现，模型可以
    自由发挥，同类问题的行数和小数位因此不一致。改成读配置后，模型拿到的是具体
    数值，评测也能对着同一份数字断言。
    """
    if not defaults:
        return []
    limits = defaults.get("limits") or {}
    fmt = defaults.get("format") or {}
    stats = defaults.get("stats") or {}
    ranking = limits.get("ranking", 10)
    single = limits.get("single", 1)
    detail = limits.get("detail", 50)
    decimals = fmt.get("decimal_places", 2)
    scale = fmt.get("percent_scale", 100.0)
    min_group = stats.get("min_group_size", 5)

    return [
        "\n## 输出约定（全系统统一，优先于你自己的习惯）",
        f"- **排名 / 榜单**类（「最多」「最高」「前十」「最热」）默认 `LIMIT {ranking}`；"
        f"用户明确说了数量（前十、三个、八年）时以用户为准。",
        f"- **全量分组**类（「各个分类分别多少」「每家出版社各多少」）**不要加 LIMIT**，"
        f"每个分组都要出现——这类问题问的是分布，砍掉两组就不是分布了。",
        f"- 问「哪一个 / 哪一本 / 谁」这类**单数**问题，只返回 `LIMIT {single}` 行。",
        f"- 明细类（逐行列出、不聚合）最多 `LIMIT {detail}` 行。",
        f"- 比率、占比、均值保留 {decimals} 位小数；计数、人数、页数这类自然整数不留小数。",
        f"- 百分比用 0-100 的数值（`{scale} * 分子 / 分母`），不带 % 号。",
        f"- 分组排名时样本量少于 {min_group} 的组不参与，用 `HAVING COUNT(*) >= {min_group}` 排除。",
    ]


def build_intent_prompt(state: TurnState, *, history: list[dict] | None = None) -> str:
    parts = []
    if history:
        parts.append("## 最近对话")
        for item in history[-3:]:
            parts.append(f"用户：{item.get('question', '')}")
            if item.get("sql"):
                parts.append(f"（上一轮 SQL：{item['sql'][:220]}）")
    parts.append(f"## 当前问题\n{state.question}")
    parts.append("请输出意图 JSON。")
    return "\n".join(parts)


def build_sql_prompt(state: TurnState, *, feedback: str = "") -> str:
    context = state.context
    parts: list[str] = []
    if context is not None:
        parts.append(render_context(context))
    question = state.intent.rewritten if state.intent and state.intent.rewritten else state.question
    parts.append(f"\n## 用户问题\n{question}")
    parts.append(f"（原始提问：{state.question}）")
    if state.intent and state.intent.defaults_applied:
        parts.append("## 已采用的默认口径\n" + "\n".join(f"- {d}" for d in state.intent.defaults_applied))
    if state.history:
        last = state.history[-1]
        parts.append(f"\n## 上一轮 SQL（用于理解「再……」「那……」这类指代）\n{str(last.get('sql', ''))[:400]}")
    if feedback:
        parts.append(f"\n## 上一次尝试失败，请修正\n{feedback}")
    parts.append("\n请输出 SQL JSON。")
    return "\n".join(parts)


def build_insight_prompt(state: TurnState, table_text: str) -> str:
    question = state.intent.rewritten if state.intent and state.intent.rewritten else state.question
    parts = [
        f"## 用户问题\n{question}",
        f"\n## 执行的 SQL\n{state.executed_sql or (state.draft.sql if state.draft else '')}",
        f"\n## 查询结果\n{table_text}",
        "\n请输出结论 JSON，只能使用上面出现的数字。",
    ]
    return "\n".join(parts)
