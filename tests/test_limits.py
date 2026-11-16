"""防刷闸门与厂商参数透传的测试。

限流器用注入的 clock，不真的等 60 秒；HTTP 层用 TestClient 打真实中间件，
只把 ``_limiter`` 换成一个很小的实例，其他一律走原路径。
"""
from __future__ import annotations

from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from app.agents.pipeline import build_deps
from app.core.config import load_settings
from app.llm.openai_compat import OpenAICompatProvider
from app.limits import RateLimiter

QUESTION = "一共多少本书"


# ---------------------------------------------------------------------------
# 限流器本体
# ---------------------------------------------------------------------------
class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def test_per_min_window_rolls_forward() -> None:
    clock = FakeClock()
    lim = RateLimiter(per_min=2, per_day=100, daily_total=1000, clock=clock)

    assert lim.check("1.1.1.1")[0] is True
    lim.hit("1.1.1.1")
    assert lim.check("1.1.1.1")[0] is True
    lim.hit("1.1.1.1")

    allowed, wait, reason = lim.check("1.1.1.1")
    assert allowed is False
    assert "过于频繁" in reason
    assert 1 <= wait <= 61

    clock.advance(61)                     # 窗口滚过去
    assert lim.check("1.1.1.1")[0] is True


def test_per_ip_day_and_global_cap() -> None:
    clock = FakeClock()
    lim = RateLimiter(per_min=0, per_day=2, daily_total=3, clock=clock)

    lim.hit("a")
    lim.hit("a")
    assert lim.check("a")[0] is False     # 单 IP 用满
    assert lim.check("b")[0] is True      # 别人还没用

    lim.hit("b")
    assert lim.check("c")[0] is False     # 全站用完
    assert "全站" in lim.check("c")[2]

    clock.advance(86400)                  # 跨天清零
    assert lim.check("c")[0] is True


def test_zero_means_unlimited() -> None:
    lim = RateLimiter(per_min=0, per_day=0, daily_total=0)
    for _ in range(50):
        lim.hit("x")
    assert lim.check("x")[0] is True


# ---------------------------------------------------------------------------
# HTTP 层
# ---------------------------------------------------------------------------
@pytest.fixture
def limited_client(monkeypatch, tmp_path):
    from app.api import server

    settings = replace(load_settings(), state_db_path=tmp_path / "runtime.sqlite")
    deps = build_deps(settings=settings, force_template=True)
    monkeypatch.setattr(server, "engine_deps", lambda: deps)
    monkeypatch.setattr(server, "_limiter", RateLimiter(per_min=1, per_day=10, daily_total=10))
    return TestClient(server.app)


def test_ask_is_throttled_but_page_is_not(limited_client: TestClient) -> None:
    first = limited_client.post("/api/ask", json={"question": QUESTION, "session_id": "t1"})
    assert first.status_code == 200

    second = limited_client.post("/api/ask", json={"question": QUESTION, "session_id": "t2"})
    assert second.status_code == 429
    assert "Retry-After" in second.headers
    assert "频繁" in second.json()["detail"]

    # 页面与观测接口不受影响，否则前端轮询会把自己刷死
    for _ in range(5):
        assert limited_client.get("/api/stats").status_code == 200
    assert limited_client.get("/").status_code == 200


def test_validation_failure_does_not_consume_quota(limited_client: TestClient) -> None:
    bad = limited_client.post("/api/ask", json={"session_id": "x"})
    assert bad.status_code == 422
    again = limited_client.post("/api/ask", json={"session_id": "x"})
    assert again.status_code == 422

    ok = limited_client.post("/api/ask", json={"question": QUESTION, "session_id": "ok"})
    assert ok.status_code == 200


def test_stream_endpoint_is_throttled_too(limited_client: TestClient) -> None:
    ok = limited_client.post("/api/ask/stream", json={"question": QUESTION, "session_id": "s1"})
    assert ok.status_code == 200
    blocked = limited_client.post("/api/ask/stream", json={"question": QUESTION, "session_id": "s2"})
    assert blocked.status_code == 429


# ---------------------------------------------------------------------------
# 厂商参数透传
# ---------------------------------------------------------------------------
def test_extra_body_is_merged_but_never_overrides_standard_fields() -> None:
    provider = OpenAICompatProvider(
        base_url="http://example.invalid/v1",
        api_key="k",
        model="qwen3.8-flash",
        max_tokens=64,
        extra_body={"enable_thinking": False, "max_tokens": 999999},
    )
    seen: dict = {}

    def fake_post(payload: dict) -> dict:
        seen.update(payload)
        return {
            "choices": [{"message": {"content": "ok"}}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 1},
            "model": "qwen3.8-flash",
        }

    provider._post = fake_post           # type: ignore[method-assign]
    provider.complete(system="s", user="u")

    assert seen["enable_thinking"] is False
    assert seen["max_tokens"] == 64       # 标准字段优先，不被 extra_body 顶掉


def test_extra_body_env_parsing(monkeypatch) -> None:
    import json

    from app.core.config import _env_json_dict

    monkeypatch.setenv("IP_LLM_EXTRA_BODY", json.dumps({"enable_thinking": False}))
    assert _env_json_dict("IP_LLM_EXTRA_BODY") == {"enable_thinking": False}

    monkeypatch.setenv("IP_LLM_EXTRA_BODY", "{oops")
    assert _env_json_dict("IP_LLM_EXTRA_BODY") == {}

    monkeypatch.setenv("IP_LLM_EXTRA_BODY", "[1,2]")
    assert _env_json_dict("IP_LLM_EXTRA_BODY") == {}
