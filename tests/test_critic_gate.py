"""自检（critic）默认关闭的守门测试。

为什么关：60 题消融里 critic 是负收益——full 45.0%（27/60）vs no_critic 48.3%（29/60），
差 2 题，超出单配置 ±1 题的波动范围。qwen 时代那组是 50.0 vs 50.8（1 题以内，属噪声），
换模型后差距变大，说明它会推翻原本答对的结论。

处理方式与 DocuMind 的 failover 一致：**能力留着，默认不用，原因写进日志**。
关闭不是"挂一个永远通过的替身"——那会让 trace 里出现 `critic: pass`，
看起来像"跑过了且通过"。关闭 = 链路上没有这个节点。
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from app.agents.pipeline import ask, build_deps, build_graph, resolve_variant
from app.core.config import Settings, load_settings

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _settings(tmp_path) -> Settings:
    return Settings(
        db_path=PROJECT_ROOT / "data" / "warehouse.sqlite",
        semantic_dir=PROJECT_ROOT / "data" / "semantic",
        state_db_path=tmp_path / "runtime.sqlite",
        cache_enabled=False,
    )


# --------------------------------------------------------------- 1. 默认关
def test_disabled_by_default(monkeypatch):
    """不设环境变量 → 默认关，且链路上没有 critic 节点。"""
    monkeypatch.delenv("IP_ENABLE_CRITIC", raising=False)
    settings = load_settings()
    assert settings.enable_critic is False
    assert resolve_variant(settings, None) == "no_critic"

    graph = build_graph(settings, resolve_variant(settings, None))
    assert "critic" not in graph._nodes, "关掉之后 critic 不该出现在图里"
    assert graph._edges["analyst"] == "reporter", "analyst 必须直连 reporter"


def test_disabled_runs_without_calling_critic(tmp_path, monkeypatch):
    """端到端：关掉时跑完一题，链路里没有 critic 这一步。

    `report.path` 是节点真实执行序列（不是配置），所以它能证明"没被调用"。
    """
    monkeypatch.delenv("IP_ENABLE_CRITIC", raising=False)
    settings = _settings(tmp_path)
    deps = build_deps(settings, force_template=True)  # variant=None → 按开关解析

    answer, report = ask("咱们库里现在一共收了多少本书啊？", deps=deps, use_cache=False)
    assert answer.status == "ok"
    assert deps.variant == "no_critic"
    assert "critic" not in report.path, report.path
    assert report.path[-1] == "reporter"


# --------------------------------------------------------------- 2. 显式开
def test_enabled_runs_the_real_critic(tmp_path, monkeypatch):
    """IP_ENABLE_CRITIC=1 → 默认变体回到 full，critic 节点真的挂在图上并被走到。"""
    monkeypatch.setenv("IP_ENABLE_CRITIC", "1")
    settings = load_settings()
    assert settings.enable_critic is True
    assert resolve_variant(settings, None) == "full"

    graph = build_graph(settings, "full")
    assert graph._nodes["critic"].__name__ == "critic"

    local = replace(_settings(tmp_path), enable_critic=True)
    deps = build_deps(local, force_template=True)
    answer, report = ask("咱们库里现在一共收了多少本书啊？", deps=deps, use_cache=False)
    assert answer.status == "ok"
    assert deps.variant == "full"
    assert "critic" in report.path, report.path


def test_explicit_variant_wins_over_env(monkeypatch):
    """评测路径必须能指定档位——否则 full / no_critic 的消融就不存在了。

    这是本开关最容易踩的坑：如果 `IP_ENABLE_CRITIC=0` 也把显式 `--variants full`
    压成 no_critic，那 README 里那张消融表再也跑不出来。
    """
    monkeypatch.setenv("IP_ENABLE_CRITIC", "0")
    settings = load_settings()
    assert resolve_variant(settings, "full") == "full"
    assert resolve_variant(settings, "no_critic") == "no_critic"

    monkeypatch.setenv("IP_ENABLE_CRITIC", "1")
    settings = load_settings()
    assert resolve_variant(settings, "no_critic") == "no_critic"


def test_unknown_variant_still_rejected():
    settings = Settings()
    with pytest.raises(ValueError):
        build_deps(settings, force_template=True, variant="nope")
