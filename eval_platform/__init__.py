"""评测中台：把散落的评测脚本收成一个入口，让「改完到底变好还是变坏」变成一条命令。

模块划分
    schema.py       数据模型 + 读任何历史格式 + 汇总指标
    suites.py       套件：接口、金标、取样、判据分派（documind / insightpilot）
    runner.py       按套件逐题打线上，产出统一格式的 run
    keyword.py      关键词判据（DocuMind 口径，组间 AND / 组内 OR）
    resultset.py    结果集等价 + 列覆盖（InsightPilot 口径）
    ragas_score.py  RAGAS 评分（独立子命令，可重跑）
    gate.py         CI 门禁：可答率跌超过阈值就 exit 1
    compare.py      diff（逐题对比）+ report（HTML 看板）
    cli.py          命令行

这个包不 import 两个项目里的任何模块（`tests/test_suites.py` 会扫描并断言），
也不写项目目录里的任何文件——所以它能原样搬进任一仓库，同时服务两个项目。
"""

__all__ = ["schema", "suites", "runner", "keyword", "resultset",
           "ragas_score", "gate", "compare", "cli"]
__version__ = "0.2.0"
