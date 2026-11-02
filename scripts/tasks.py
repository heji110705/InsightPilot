"""Makefile 的 Windows 等价物：同一批任务，不依赖 make。

`make` 在 Windows 上默认不可用，而招聘方 / 同学拿到仓库第一件事往往是「跑起来看看」。
两边任务名保持一致（`python scripts/tasks.py test` ≈ `make test`），README 只写一份。
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = sys.executable


def _run(cmd: list[str], *, env: dict[str, str] | None = None) -> int:
    merged = dict(os.environ)
    merged.update(env or {})
    print("$ " + " ".join(cmd), flush=True)
    return subprocess.call(cmd, cwd=ROOT, env=merged)


def task_install() -> int:
    return _run([PY, "-m", "pip", "install", "-r", "requirements.txt"])


def task_demo() -> int:
    # 规则基线：不联网、不花钱、不需要 Key。演示现场最稳的一条。
    return _run(
        [PY, "-m", "uvicorn", "app.api.server:app", "--port", "8000"],
        env={"IP_LLM_MODE": "stub"},
    )


def task_run() -> int:
    return _run([PY, "-m", "uvicorn", "app.api.server:app", "--port", "8000"])


def task_doctor() -> int:
    return _run([PY, "scripts/doctor.py"])


def task_test() -> int:
    return _run([PY, "-m", "pytest", "tests/", "-q"])


def task_eval() -> int:
    return _run([
        PY, "scripts/run_eval.py",
        "--variants", "full,single,no_schema,no_critic",
        "--mode", "auto", "--out", "docs/",
    ])


def task_eval_quick() -> int:
    return _run([
        PY, "scripts/run_eval.py",
        "--variants", "full", "--limit", "20",
        "--mode", "auto", "--out", "_work/quick",
    ])


def task_docker() -> int:
    return _run(["docker", "compose", "up", "--build"])


def task_clean() -> int:
    removed = 0
    for path in ROOT.rglob("__pycache__"):
        shutil.rmtree(path, ignore_errors=True)
        removed += 1
    for path in (ROOT / ".pytest_cache", ROOT / "_work" / "quick"):
        if path.exists():
            shutil.rmtree(path, ignore_errors=True)
            removed += 1
    print(f"清理完成：{removed} 个目录")
    return 0


TASKS = {
    "install": task_install,
    "demo": task_demo,
    "run": task_run,
    "doctor": task_doctor,
    "test": task_test,
    "eval": task_eval,
    "eval-quick": task_eval_quick,
    "docker": task_docker,
    "clean": task_clean,
}


def main() -> int:
    parser = argparse.ArgumentParser(description="InsightPilot 常用任务（Makefile 的 Windows 版）")
    parser.add_argument("task", choices=sorted(TASKS), help="要执行的任务")
    args = parser.parse_args()
    return TASKS[args.task]()


if __name__ == "__main__":
    raise SystemExit(main())
