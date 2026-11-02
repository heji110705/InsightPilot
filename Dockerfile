# 两段式构建：依赖层与代码层分开，改代码不会触发重新装包。
FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# 依赖先行：这一层只随 requirements.txt 变化
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# 只复制运行所需。tests/ 与 docs/ 不进镜像——镜像越小，冷启动越快，
# 也避免把评测数据当成运行时依赖。
COPY app/ ./app/
COPY web/ ./web/
COPY data/ ./data/
COPY scripts/ ./scripts/

# 非 root 运行：容器里以 root 跑 Web 服务是最常见的低级问题
RUN useradd --create-home --uid 10001 pilot \
    && chown -R pilot:pilot /app
USER pilot

EXPOSE 8000

# 健康检查直接打业务接口，而不是只测端口
HEALTHCHECK --interval=30s --timeout=4s --start-period=15s --retries=3 \
    CMD python -c "import sys,urllib.request; \
sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=3).status == 200 else 1)"

CMD ["python", "-m", "uvicorn", "app.api.server:app", "--host", "0.0.0.0", "--port", "8000"]
