# eval_platform · 评测中台

把散落的评测脚本收成一个入口，让「这次改完到底变好还是变坏」变成一条命令、一个退出码。

解决的问题不是"没有评测"，而是评测结果没法比：json 格式两三种、参数各不同、评分脚本
散在两个仓库里。要知道哪题新错了只能人眼对。这个包只干一件事——**给评测加一个统一的壳**，
并让壳能同时服务 DocuMind 与 InsightPilot：

| 套件 | 接口 | 金标 | 判据 |
|---|---|---|---|
| `documind` | `POST /v1/ask` | 124 题（94 可答 + 30 必拒） | 关键词组命中 + 必拒题必须拒答 |
| `insightpilot` | `POST /api/ask` | 60 题（T1–T6 各 10） | 结果集等价 + 列覆盖 |

---

## 快速开始

```bash
cd C:\Users\23840\Desktop\shixijianli

python -m eval_platform list                       # 有哪些 run，各自多少分
python -m eval_platform show 005140                # 一个 run 的全套指标
python -m eval_platform diff 150553 005140          # 逐题对比：哪题新错了
python -m eval_platform report                     # 出 HTML 看板
python -m eval_platform run --tag v7.3 --limit 20 --yes          # 打 DocuMind（花钱）
python -m eval_platform score eval_runs/xxx.json                 # 补打 RAGAS（免费，可重跑）
python -m eval_platform gate eval_runs/baseline.json eval_runs/xxx.json   # 门禁
```

`list / show / diff / report / gate` 全是纯本地，不联网、不花钱。
只有 `run` 会打线上（要 `--yes`），`score` 会调裁判模型但**不重新采集**。

---

## 七个命令

| 命令 | 干什么 | 要不要钱 |
|---|---|---|
| `list` | 扫 `eval_runs/`，列出每个 run 的题数、可答率、p50 | 不要 |
| `show` | 一个 run 的全部指标（含 RAGAS 分项、列覆盖率） | 不要 |
| `diff A B` | 两次 run 逐题对齐，分「新对 / 新错 / 仍对 / 仍错」，**新错的题把两次答案并排打出来** | 不要 |
| `run` | 按套件逐题打线上接口，判分、计时、记成本，落一个统一格式的 run | **要** |
| `score RUN` | 读 run 里已存的 answer/contexts，补打 RAGAS，结果写回 `Item.ragas` | 裁判模型的钱，很少 |
| `gate BASE NEW` | 比可答率，跌超过阈值就 `exit 1`（CI 用） | 不要 |
| `report` | 读所有 run，生成单文件 HTML：趋势线 + 逐题矩阵 | 不要 |

`A B` 这类参数可以只写文件名片段（如 `150553`），不用敲全路径。

---

## 为什么 `score` 不塞进 `run`

采集和评分是两种成本：**采集花钱，评分不花钱但慢**。

- `ragas` + `datasets` 是好几百兆的依赖，塞进 `run` 等于每次采集都要把它们装好；
- 裁判模型或指标口径一改，同一批 answer 就得重打分。绑在一起，改一次评分口径就要重打一遍线上。

所以 `run` 只负责"问 + 判 + 记"，`score` 只负责"对已存的 answer 打分"，可反复跑。

**RAGAS 口径（别改）**

1. **拒答题也进分母。** 上一版把 refused 过滤掉，"拒答是否正确"就完全没进统计。
   现在拒答题即使没有 contexts 也收进样本。
2. **拒答且没有编造 → faithfulness 记 1.0。** "没有编造"本身就是忠实；记 0 会把正确拒答
   惩罚成幻觉，方向反了。拒答但出现了 `must_absent` 的说法 → 0.0。
3. **`context_recall` 用确定性代理。** RAGAS 原版要参考答案，v2 金标只有 `must_include`
   关键词组。不自己编参考答案，改判"每个关键词组是否至少有一个别名出现在召回上下文里"
   （组间 AND），单列命名 `context_recall_keyword_proxy`，不冒充原版指标。
4. **`answer_relevancy` 换成了 `answer_relevancy_llm`。** RAGAS 原版要 embedding
   （反向生成问题再算余弦），而 DeepSeek **没有 embeddings 端点**（实测 `POST
   /v1/embeddings` → 404）。改成让裁判模型直判「答案是否切题」，指标名如实加 `_llm`
   后缀，**不与历史表里的 `answer_relevancy 0.711` 可比**。

**裁判挂了必须报错，不许给假分数。** `ragas` 的 `evaluate(raise_exceptions=False)`
会把 401 吞成 NaN 或 0.0：实测一把无效 key 跑完 3 题，一个异常都没抛，报告里躺着
`context_precision: 0.0` —— 一个"看起来很正常的低分"。所以 `score` 开跑前先发一个
1-token 对话请求（约 ¥0.0001）把端点问清楚，跑完再查 NaN 比例。

**退出码分得开，别只看"非 0"**

| 码 | 含义 | 先查什么 |
|---|---|---|
| 0 | 评完并落盘 | — |
| 3 | 没有可评样本（没有上下文，也不是必拒题） | 是不是拿 insightpilot 的 run 来跑 RAGAS |
| 4 | 评不了：ragas 没装 / judge 构造失败 | `pip install -U ragas datasets langchain-openai` |
| 5 | 评不了：裁判端点不通（key / base / 模型名） | `--ragas-key`、`--ragas-base`、模型是否在该端点 |

为什么要分 4 和 5：这两种失败以前都是一句 `[skip]` + `exit 3`，日志里长得几乎一样，
于是「依赖没装」会被当成「预检生效」——报数的人不知道自己在看哪一种。

---

## 三条口径，写死了，改之前先想清楚

**1. 可答率的分母是可答题，不含必拒题**
DocuMind 124 题 = 94 可答 + 30 必拒。`answerable_rate` = 可答通过数 / 94。
InsightPilot 的题型是 T1–T6，全部算可答（`type != refuse` 即分母）。
把必拒题混进分母会让"拒答能力强"的项目在数字上吃亏。

**2. 请求失败的题不进分母**
一次 HTTP 504 的真实含义是「网络挂了」，不是「答错了」。混进去会让分数看起来是质量下降。
`metrics.n_error` 单独报出来，谁想看都能看到。**429 不算答错**：撞到限流会按
`Retry-After` 等一会儿再试（最多 3 次），仍然失败才记 `error`。

**3. p95 用 `sorted(lat)[int(len*0.95)-1]`，题数 < 20 时不给值**
这是老脚本 `_p1/ragas_eval.py:basic_stats` 的公式，刻意不换成统计学标准分位数——
新中台要和历史 run 比，口径必须逐位对齐，否则 diff 里会出现「同一批数据 p95 变了」的假变化。

这三条都有单测钉着（`tests/test_core.py`），改任何一条都会红。

**关键词判据**：组间 AND、组内 OR。`[["28%"], ["订单"]]` 要求两样都出现——写成
「任一命中即算对」会让分数虚高。这条来自 DocuMind 的 `evalutil.hit`，同样有单测。

**结果集判据**：列名、行序、别名都不参与；数值 `1e-6` 相对容差；`None` 与空串不等；
`1` 与 `"1"` 不等。另有**列覆盖**作第二指标：容忍"多给了几列"，`cover - ex` 就是
「答对但投影没收住」的比例。两个数并列报，谁都别想单独代表质量。

---

## run 文件格式

```json
{
  "schema": 1,
  "suite": "documind",
  "tag": "v7.3",
  "created_at": "2026-11-19T21:50:00",
  "config": {"base_url": "http://8.163.90.97", "mode": "mix", "gold": "eval_set_v2.json"},
  "items": [
    {"id": "Q001", "type": "fact", "question": "...", "answer": "...", "refused": false,
     "elapsed_ms": 1234, "cost_cny": 0.001, "tokens": 3000,
     "kw_hit": true, "kw_leak": false, "refuse_ok": null,
     "must": [["一单一书"]], "judge": null, "ragas": {"faithfulness": 0.91}}
  ],
  "metrics": {"n": 124, "answerable_rate": 0.872, "...": "..."}
}
```

`judge` 是套件私有判据明细（InsightPilot 才有）：

```json
{"ex": true, "cover": true, "diff": "结果一致", "cover_diff": "覆盖一致（多出 1 列：占比）",
 "sql": "SELECT ...", "gold_sql": "SELECT ...", "status": "ok", "attempts": 1, "model": "deepseek-chat"}
```

**旧的两种格式都能读，不用迁移、不用改老文件：**

- `{"meta": {...}, "items": [...]}` —— 采集结果
- `{"meta": {...}, "basic": {...}, "ragas": {...}}` —— 评分结果（没有逐题，只能看指标不能 diff）

`basic.keyword_hit` 会自动映射成 `answerable_rate`，所以新旧文件在 `list` 里是同一列。

---

## 两个套件的差别

```bash
python -m eval_platform run --suite insightpilot --limit 10 --yes        # 打线上 Demo
python -m eval_platform run --suite insightpilot --base-url http://127.0.0.1:8000 \
       --limit 20 --sleep 0.5 --yes --out eval_runs/ci-latest.json       # 打本地实例
```

**取样**：`--limit` 不是"取前 N 题"，而是**分层 + 层内等距**。

先用 `python -m eval_platform show` 看一眼就能发现问题：金标是按题型分块排列的
（DocuMind 前 16 题全是 schema/口径/陷阱题，InsightPilot 是 T1 十题、T2 十题……），
取前 N 得到的子集偏难——实测 20 题取头 16 个 fact 只有 68.75%，全量 124 题是 87.2%。
子集偏不致命（baseline 与新 run 用同一批题，回归照样看得出来），但它会让门禁对
"文档类问题变差"完全失明：那 78 道题根本没进过 CI。

所以先按题型比例分层（DocuMind 的 `fact:refuse = 94:30`，必拒题不能被截掉；
InsightPilot 六类均分），再在每层里等距取（跨度铺满整层）。
取样确定、不随机，同一个 `--limit` 每次抽到同一批题——baseline 与新 run 才可比。

**默认间隔 `--sleep`**：DocuMind 2s；InsightPilot **21s**。后者是因为公开 Demo 有
「每 IP 每分钟 3 次」的防刷闸门（`projects/insightpilot/app/limits.py`）。打本地实例时
显式传 `--sleep 0.5` 把它降下来。

**成本**：InsightPilot 的响应不返回 token/成本，所以 `cost_per_q_cny` 是 `None` 而不是 `¥0`
——0 会被读成"问数不要钱"，那是另一种假数字。

---

## CI 门禁

```bash
python -m eval_platform run --tag ci-${GITHUB_SHA:0:7} --limit 20 --yes --quiet \
       --out eval_runs/ci-latest.json
python -m eval_platform gate eval_runs/baseline.json eval_runs/ci-latest.json --max-drop-n 2
```

`gate` 的规则：

1. **掉满 `--max-drop-n N` 题** → `exit 1`（`--max-drop-n 2` = 掉 1 题放过、掉 2 题红）；
   或者用 `--max-drop X` 按百分点判。两个阈值**互斥**，同时给会被 argparse 挡下；
2. 两个 run 的套件必须相同（EX 与关键词命中不可比）；
3. 新 run 的有效题数不得低于 baseline 的 80%（默认）——请求失败不进分母，一串 429
   会让分母从 20 缩到 12，可答率反而更好看，那是"少考了几道题"。

**为什么 CI 用「掉几题」而不是 pp**：20 题的 run 里只有 16 道可答题，**2pp = 0.32 题**。
pp 在这个样本量上没有意义，实际效果是"掉 1 题就红"（=6.25pp），而同配置复跑的正常
波动就是 0~2 题——于是门禁红得毫无信息量，人很快学会无视它。改成 `--max-drop-n 2`
之后，宽容度 2 题，与实测波动对齐；真正的退化（≥3 题）仍然会红。

pp 阈值留给样本量够大的场合（≥100 题时 2pp 才有意义）。两种规则都不许调 baseline 来"修"。

**baseline 只许人工更新**。`eval_runs/baseline.json` 是仓库里的锚点，`.gitignore` 里
专门给它开了例外；`run --out eval_runs/baseline.json` 会被直接拒绝（自动覆盖等于让门禁
自己把自己校准成永远绿）。确认是有意变更后才手动 `cp` 过去。

**两个仓库都接了**，都只在 push 到 `main`/`master` 或手动 `workflow_dispatch` 时跑：

| 仓库 | 打哪 | 说明 |
|---|---|---|
| DocuMind | 线上 `http://8.163.90.97/v1/ask` | 20 题约 ¥0.12；不需要装任何依赖（评测中台只用标准库） |
| InsightPilot | **runner 本机起的实例** | 线上 Demo 有 3 次/分 + 10 次/天/IP + 全站 40 次/天 的闸门，20 题必然撞 429，而且会吃掉真实访客的额度。CI 里 `IP_RATE_ENABLED=0 IP_CACHE=0` 起同一个 commit 的实例再打 |

---

## 测试

```bash
python -m unittest discover -s eval_platform/tests -t .    # 117 条，0.08 秒
python eval_platform/tests/test_core.py                    # 也可以单文件跑
```

| 文件 | 钉住什么 |
|---|---|
| `test_core.py` | 关键词判据、三条口径、旧格式兼容、diff、看板过滤 |
| `test_resultset.py` | 结果集等价与列覆盖（与 InsightPilot 仓库里的 12 条规则一一对应） |
| `test_suites.py` | 金标归一化、取样比例、请求体、响应解析、真值结果集、**禁止 import 项目模块** |
| `test_score.py` | RAGAS 口径：拒答记 1.0 并进分母、代理召回、NaN→None、裁判端点预检（mock 掉 ragas，CI 不必装） |
| `test_gate.py` | 阈值边界（正好 2pp 通过）、分母护栏、套件护栏 |

---

## 怎么搬进仓库

整个目录原样拷到仓库根，`cli.ROOT` 取的是"本文件的上一级"，搬进去正好是仓库根，**不用改**：

```
<repo>/
  eval_platform/          ← 原样拷
  eval_runs/baseline.json ← 门禁锚点（.gitignore 里给它开了例外）
```

`SCAN_DIRS` 在仓库副本里只留 `eval_runs`：作者工作区还有 `_p1/runs`（历史脚本的产物），
公共仓库里没有，删掉那一行免得每次都要解释它为什么不存在。

**为什么独立成一个包而不是塞进某个仓库的 `scripts/`**：里面没有一句 import 两边的业务
模块（判据是抄的一份 + 单测钉住），`test_suites.py::test_no_project_imports` 会扫描整个
包，出现 `import app...` 就红。所以它能同时服务两个项目，将来也能单独开源出去。
