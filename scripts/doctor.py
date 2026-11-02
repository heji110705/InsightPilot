"""环境自检：一条命令回答「这台机器现在能不能跑」。

为什么单独写一个脚本
--------------------
这个项目有三层隐式依赖：Python 依赖、数据仓库文件、语义层 YAML。任何一层缺失
都会在运行时以「表不存在」「指标召回为空」的形式暴露，排查要翻三四个模块。
把检查前置成一张表，比在 README 里写五条排查建议有用。

设计上刻意不 import 项目自己的模块（除了最后读配置），因为它要在**依赖装了一半**
的情况下也能跑起来，报出「缺了哪一半」。退出码 0=全部就绪，1=有阻塞项。
"""
from __future__ import annotations

import importlib
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

OK = "ok"
WARN = "warn"
FAIL = "fail"


@dataclass
class Check:
    name: str
    status: str
    detail: str


def check_python() -> Check:
    version = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    if sys.version_info < (3, 11):
        return Check("Python 版本", FAIL, f"{version}，需要 3.11+（用了 X | None 语法）")
    return Check("Python 版本", OK, version)


def check_deps() -> Check:
    required = {
        "fastapi": "Web 框架",
        "uvicorn": "ASGI 服务器",
        "pydantic": "数据模型",
        "sqlglot": "SQL AST 校验",
        "yaml": "语义层解析",
        "httpx": "模型调用",
    }
    missing = []
    for module, purpose in required.items():
        try:
            importlib.import_module(module)
        except Exception:
            missing.append(f"{module}（{purpose}）")
    if missing:
        return Check("依赖", FAIL, "缺 " + "、".join(missing) + "；跑 pip install -r requirements.txt")
    return Check("依赖", OK, f"{len(required)} 个核心包齐全")


def check_data() -> Check:
    db = ROOT / "data" / "warehouse.sqlite"
    if not db.exists():
        return Check("数据仓库", FAIL, f"缺少 {db.relative_to(ROOT)}；跑 python scripts/etl.py")
    try:
        with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as conn:
            tables = [
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type IN ('table','view') ORDER BY name"
                )
            ]
            facts = 0
            for table in ("fact_rating", "fact_comment", "fact_click", "fact_shelf"):
                if table in tables:
                    facts += conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    except sqlite3.Error as exc:
        return Check("数据仓库", FAIL, f"打不开：{exc}")
    size_mb = db.stat().st_size / 1024 / 1024
    if facts == 0:
        return Check("数据仓库", FAIL, "事实表是空的；重新跑 scripts/etl.py")
    return Check("数据仓库", OK, f"{len(tables)} 个对象 / 事实表 {facts} 行 / {size_mb:.1f} MB")


def check_semantic() -> Check:
    root = ROOT / "data" / "semantic"
    expected = {
        "tables.yml": "表卡片",
        "metrics.yml": "指标口径",
        "fewshots.yml": "范例",
        "synonyms.yml": "同义词",
        "defaults.yml": "输出约定",
    }
    missing = [f"{name}（{purpose}）" for name, purpose in expected.items()
               if not (root / name).exists()]
    if missing:
        return Check("语义层", FAIL, "缺 " + "、".join(missing))
    return Check("语义层", OK, "、".join(expected))


def check_llm() -> Check:
    env = _read_env()
    mode = (env.get("IP_LLM_MODE") or "stub").strip().lower()
    key = (env.get("IP_LLM_API_KEY") or "").strip()
    if mode == "stub":
        return Check("推理模式", OK, "stub（规则基线，不联网；界面/评测全部可用）")
    if not key:
        return Check("推理模式", FAIL, "mode=api 但 IP_LLM_API_KEY 为空，会静默回退模板")
    model = env.get("IP_LLM_MODEL", "deepseek-chat")
    # 不回显 Key 的任何片段：自检输出经常被贴进 issue / 群里。
    return Check("推理模式", OK, f"api / {model} / key 已配置（{len(key)} 字符）")


def check_writable() -> Check:
    target = ROOT / "data" / "runtime.sqlite"
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        probe = target.with_suffix(".probe")
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        return Check("写入权限", FAIL, f"data/ 不可写：{exc}")
    return Check("写入权限", OK, f"可写（trace 落到 {target.name}）")


def _read_env() -> dict[str, str]:
    """读 .env，不依赖 python-dotenv；解析失败就当没配。"""
    env: dict[str, str] = {}
    path = ROOT / ".env"
    if not path.exists():
        return env
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        env[key.strip()] = value.strip()
    return env


def main() -> int:
    checks = [
        check_python(),
        check_deps(),
        check_data(),
        check_semantic(),
        check_llm(),
        check_writable(),
    ]
    width = max(len(item.name) for item in checks)
    mark = {OK: "OK  ", WARN: "WARN", FAIL: "FAIL"}
    print("\nInsightPilot 环境自检\n" + "-" * 64)
    for item in checks:
        print(f"[{mark[item.status]}] {item.name.ljust(width)}  {item.detail}")
    print("-" * 64)

    failures = [item for item in checks if item.status == FAIL]
    if failures:
        print(f"{len(failures)} 项阻塞。按上面的提示处理后重跑本脚本。\n")
        return 1
    print("全部就绪：python -m uvicorn app.api.server:app --port 8000\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
