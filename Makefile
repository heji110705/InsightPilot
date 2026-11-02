# InsightPilot —— 常用命令
#
# 这一层的存在理由：README 里的命令一旦超过三条，就没人照着敲了。
# 所有会写进简历/文档的操作，都必须有一条能直接跑的命令。
#
# Windows 没有 make？用 `python scripts/tasks.py <同名任务>`，两边行为一致。

PY ?= python

.PHONY: help install demo run doctor test test-fast eval eval-quick docker docker-down clean

help:
	@echo "install    安装依赖"
	@echo "demo       零配置起服务（规则基线，不需要 API Key）"
	@echo "run        起服务（读取 .env）"
	@echo "doctor     环境自检：依赖/数据/语义层/Key 是否就绪"
	@echo "test       全量测试"
	@echo "eval       全量评测：四组消融，两轮"
	@echo "eval-quick 快速评测：只跑 full，20 题"
	@echo "docker     容器化启动"
	@echo "clean      清理缓存与运行时产物"

install:
	$(PY) -m pip install -r requirements.txt

# 不依赖 .env、不联网、不花钱：面试现场最稳的一条命令
demo:
	IP_LLM_MODE=stub $(PY) -m uvicorn app.api.server:app --port 8000

run:
	$(PY) -m uvicorn app.api.server:app --port 8000

doctor:
	$(PY) scripts/doctor.py

test:
	$(PY) -m pytest tests/ -q

test-fast:
	$(PY) -m pytest tests/ -q -x --ignore=tests/test_etl.py

eval:
	$(PY) scripts/run_eval.py --variants full,single,no_schema,no_critic --mode auto --out docs/

eval-quick:
	$(PY) scripts/run_eval.py --variants full --limit 20 --mode auto --out _work/quick

docker:
	docker compose up --build

docker-down:
	docker compose down

clean:
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
	rm -rf .pytest_cache _work/quick
