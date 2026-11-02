# InsightPilot · 多智能体自助问数平台

业务同学用中文问一句，系统自动完成 **理解意图 → 定位数据 → 写 SQL → 安全执行 → 统计选图 → 自检 → 给出结论与口径说明**，全过程可追溯、可评测、有安全边界。

```
"评分人数最多的前 10 本书是哪些？"
      ↓
SELECT title AS 书名, rating_cnt AS 评分人数
FROM v_book_overview ORDER BY rating_cnt DESC LIMIT 10
      ↓
结论：我的阿勒泰 的 评分人数 最高，为 29  ·  第 2 名：笑傲江湖（全四册），评分人数 26
图表：条形图 ｜ 口径脚注：结果 10 行
```

---

## 一句话设计主张

> 我不相信"大模型能写 SQL"这件事。
> 我只相信"在受约束的语义层上，执行准确率能从 A% 提到 B%"，以及"每一个数字都能追回到一行结果"。
> 所以这个项目的重心不在调 prompt，在**约束**和**度量**。

具体落在三条工程约束上：

1. **语义层前置**：不给模型整库 DDL，只给检索出来的表卡片 + 指标口径 + 范例 SQL。
   口径（如"评分是 0-10 分不是 1-5 星"）写进配置，不指望模型每次都想起来。
2. **数字由代码算**：统计、派生指标、结论里的每个数都由 `app/agents/stats.py` 计算；
   模型只负责措辞，且措辞里的数字要回查结果集，查不到就丢弃它的表述。
3. **效果必须可度量**：60 题评测集 + 结果集等价判定 + 四组消融配置。
   没有对照组的"多 Agent 架构"只是看起来复杂。

---

## 快速开始

```bash
# 1. 依赖（Python 3.12）
pip install -r requirements.txt

# 2. 生成数据仓库（从源库转换，只读源库）
python scripts/etl.py

# 3. 起服务
python -m uvicorn app.api.server:app --port 8000
# 打开 http://127.0.0.1:8000

# 4. 跑测试（219 个）
python -m pytest tests/ -q

# 5. 跑评测
python scripts/run_eval.py --variants full,single,no_schema,no_critic --out docs/
```

**不配 LLM Key 也能完整运行**：默认 `IP_LLM_MODE=stub`，走规则 + 指标模板的基线路径，
界面、trace、评测、SSE 全部可用。配 Key 后一条命令切到模型模式：

```bash
# .env
IP_LLM_MODE=api
IP_LLM_BASE_URL=https://api.deepseek.com/v1     # 任何 OpenAI 兼容端点
IP_LLM_API_KEY=sk-xxxx
IP_LLM_MODEL=deepseek-chat
```

---

## 架构

```
用户提问
   │
① Intake ──(需澄清)──▶ 回问用户
   │            (澄清/超范围走这里，不硬答)
   ├─▶ ② Linker ──▶ ③ SQLCraft ──▶ ④ Executor
   │      语义层          生成 SQL        校验+沙箱执行
   │      三路召回                          │ 报错回灌 ≤3 次
   │                                        ▼
   │                                    ⑤ Analyst ──▶ ⑥ Critic
   │                                    统计/选图/结论    数字·样本·口径
   │                                        ▲              │ 不通过打回 ≤2 次
   │                                        └──────────────┘
   │                                                       ▼ 通过
   └────────────────────────────────────────────── 答复：SQL + 表 + 图 + 口径
```

| 层 | 目录 | 职责 |
|---|---|---|
| 接入 | `app/api/` | FastAPI 路由、SSE 节点级事件流、静态页 |
| 编排 | `app/harness/` | 状态图引擎：节点注册、条件边、检查点、max_steps 保护 |
| Agent | `app/agents/` | 6 个 Agent + 统计 + 提示词 + 两个 Inferencer |
| 语义 | `app/semantic/` | YAML 加载、手写 BM25、三路召回、预算裁剪 |
| 执行 | `app/db/` | sqlglot AST 白名单校验、只读沙箱、超时与行长上限 |
| 观测 | `app/obs/` | trace 落 SQLite + 树形可视化 |
| 评测 | `app/eval/` | 结果集等价判定、消融运行器、报告生成 |
| 数据 | `data/` | 星型模型仓库快照 + 语义层配置 + 60 题 gold 集 |

---

## 关键实现说明

### 为什么手写状态图引擎，而不是直接用 LangGraph

想讲清楚三件事就必须自己写一遍：状态怎么合并、条件边怎么路由、检查点怎么恢复。
`app/harness/graph.py` 只有约 270 行，但覆盖了重试环、打回环、异常隔离、max_steps 死循环保护。
面试时被问"LangGraph 的 StateGraph 是什么"，能讲实现比能讲 API 有说服力。

（节点函数是纯函数 `(State, Context) -> Patch`，所以换成任何编排框架都只是换装配代码。）

### 为什么不用向量库

表卡片 + 指标 + 范例一共不到 200 条。BM25 在这个量级上召回足够，且**完全可解释**——
每条召回都记了 `hit_reason`（`name_exact` / `alias:评分人数` / `bm25:0.83` / `join:fact_rating`），
trace 里能直接看出"为什么召回了这张卡"。向量检索给不出这个。

### 为什么 SQL 校验要过 AST

正则黑名单必被绕过（注释、大小写、函数嵌套、Unicode 空白）。
`guard.py` 用 sqlglot 解析后判定：根节点必须是单条 SELECT、禁 DDL/DML/PRAGMA/ATTACH、
表名白名单、CTE 名豁免、无 LIMIT 时外包一层行数上限。
另外以只读 URI 打开数据库 + `PRAGMA query_only=ON`，四层防护。

### 为什么图表服务端渲染 SVG

前端零 CDN 依赖，断网、内网、离线演示都不会白屏。638 行原生 JS 里没有一行 `import`。

---

## 评测（两轮重复实验，DeepSeek-chat）

> 60 题，6 类各 10 题，判定标准是**结果集等价**（列数相同、行序无关、数值容差 1e-6），不是 SQL 字符串比对。
> 每个配置跑 **2 轮**，下表为均值；单轮数据见 `docs/03-评测报告.md`，重复性分析见 `docs/03b-重复性检验.md`。

| 配置 | EX（两轮均值） | 两轮原始值 | 规则基线对照 |
|---|---|---|---|
| full（6 Agent 全开） | **43.3%** | 41.7% / 45.0% | 18.3% |
| no_critic（关自检） | 44.2% | 45.0% / 43.3% | 18.3% |
| no_schema（关语义层检索） | 39.2% | 38.3% / 40.0% | 16.7% |
| single（无检索/无重试/无自检） | 39.2% | 40.0% / 38.3% | 16.7% |

**怎么读这张表（重要，别跳过）**：
- **同一配置重复运行的 EX 极差是 3.3 个百分点**（full 两次 41.7% / 45.0%）。
  四组配置的组间极差是 5.0 个百分点，只比噪声大一点，每配置仅 2 次运行。
- 所以只能说：去掉语义层检索、或退化成单 Agent，各掉约 4 个点，**方向符合预期但不显著**；
  **去掉 Critic 反而高 0.9 个点，在噪声内，等于这个模块没测出收益。**
- 本仓库**不声称**"多 Agent 编排带来准确率提升"——现有数据支撑不了这句话。
- 规则基线 18.3% 与模型模式 43.3% 之间 +25 个点的差距，是整张表里唯一远超噪声的对比。

**失败构成**：35 个失败里 26 个是「列数不同」（模型多给了未被问到的列），
5 个取值不同、4 个行数不同。前者是可以靠提示词约束收掉的，说明 43.3% 更多是提示词问题
而非模型能力上限。

分类型：T1 75% / T2 55% / T3 50% / T4 50% / T5 30% / **T6 口径陷阱 0%**
（T6 刻意含糊，gold 选定了明确口径，而系统被要求含糊时先反问，属于设计取舍的必然扣分）。

复现：
```bash
python scripts/run_eval.py --variants full,single,no_schema,no_critic --mode auto --out docs/
python scripts/run_eval.py --variants full --mode auto --out _work/repeat      # 重复性
python scripts/repeatability.py --runs docs _work/repeat _work/repeat2 --out docs/03b-重复性检验.md
```

---

## 能力边界（不吹的部分）

| 项 | 实际情况 |
|---|---|
| 数据规模 | 万级事实表（250 书 / 4202 评分 / 9028 点击 / 1976 评论 / 2532 书架），**不是海量数据** |
| 时间跨度 | 快照只覆盖 2026-10-18 与 2026-11-01 两天，趋势类问题区分度有限 |
| 部署形态 | 单机版，没有分布式、没有高并发压测，SQLite 单文件 |
| 多轮 | 支持指代消解（"再按出版社拆一下"），但不做跨会话长期记忆 |
| 权限 | 单租户，没有行级权限与数据脱敏 |
| LLM 数字 | DeepSeek-chat 两轮均值 43.3%；消融差异在重复波动内，**不声称架构带来提升** |

---

## 目录

```
insightpilot/
├─ app/
│  ├─ api/        server.py（8 个路由，含 SSE）
│  ├─ harness/    graph.py 状态图引擎 / context.py / checkpoint.py
│  ├─ agents/     nodes.py 6 个 Agent / stats.py 统计 / inferencers.py 两条推理路径
│  ├─ semantic/   loader.py / bm25.py / retriever.py
│  ├─ db/         guard.py AST 校验 / repo.py 只读沙箱
│  ├─ obs/        trace.py 落库与回放
│  └─ eval/       compare.py / runner.py / report.py
├─ data/          warehouse.sqlite / semantic/*.yml / gold.jsonl
├─ web/           index.html + app.js + style.css（零构建、零 CDN）
├─ scripts/       etl.py / run_eval.py / check_gold.py / check_recall.py
├─ tests/         219 个用例
└─ docs/          项目方案 / 实现契约 / 评测报告 / 架构取舍 / 开发纪实 / 简历素材
```

## 文档索引

| 文件 | 内容 |
|---|---|
| `docs/00-项目方案.md` | 立项思路、与同类项目的差异、阶段计划 |
| `docs/01-实现契约.md` | 并行开发时的接口契约（含 YAML 格式规范） |
| `docs/02-架构与取舍.md` | 分层设计、8 项技术选型的"为什么不用 X" |
| `docs/03-评测报告.md` | 四组消融、分类型表现、失败清单、局限性 |
| `docs/04-开发纪实.md` | 数据坑与 6 个真实 bug 的定位过程 |
| `docs/05-简历素材.md` | 三版简历条目 + 16 个可能被追问的点 |
