"""HTTP 层测试。

三个原则：

1. 打真实引擎（模板推理器、真实数仓），不 mock ``ask()`` —— 接口测试要能证明
   "服务包对了"，而不是"服务调了一个假函数"。
2. 状态库落在 ``tmp_path``：不污染仓库里的 ``data/runtime.sqlite``，也不会被上一次
   运行留下的缓存改变路径（缓存命中时 ``report.path`` 只有一个 ``cache``）。
3. 唯一的接缝是 ``server.engine_deps``，其余全走 HTTP。
"""
from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.agents.pipeline import build_deps
from app.core.config import load_settings
from app.harness.graph import RunReport

COUNT_QUESTION = "一共多少本书"


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def deps(tmp_path_factory: pytest.TempPathFactory):
    state_db = tmp_path_factory.mktemp("api-state") / "runtime.sqlite"
    settings = replace(load_settings(), state_db_path=state_db)
    return build_deps(settings=settings, force_template=True)


@pytest.fixture(scope="module")
def client(deps):
    from app.api import server

    original = server.engine_deps
    server.engine_deps = lambda: deps
    try:
        with TestClient(server.app) as test_client:
            yield test_client
    finally:
        server.engine_deps = original


@pytest.fixture(scope="module")
def first_ask(client: TestClient) -> dict:
    """先跑一轮，后面的 trace / stats 用例才有数据可查。"""
    res = client.post("/api/ask", json={"question": COUNT_QUESTION, "session_id": "test-s1"})
    assert res.status_code == 200, res.text
    return res.json()


def _parse_sse(text: str) -> list[tuple[str, dict]]:
    events: list[tuple[str, dict]] = []
    for block in text.replace("\r\n", "\n").split("\n\n"):
        name, data = "message", []
        for line in block.split("\n"):
            if line.startswith("event:"):
                name = line[len("event:"):].strip()
            elif line.startswith("data:"):
                data.append(line[len("data:"):].strip())
        if data:
            events.append((name, json.loads("\n".join(data))))
    return events


# ---------------------------------------------------------------------------
# 自检
# ---------------------------------------------------------------------------
def test_health_returns_engine_selfcheck(client: TestClient) -> None:
    res = client.get("/api/health")
    assert res.status_code == 200
    body = res.json()
    for key in ("status", "mode", "tables", "metrics", "db_path", "variant", "inferencer"):
        assert key in body, key
    assert body["status"] == "ok"
    assert body["tables"] > 0 and body["metrics"] > 0
    assert body["db_path"].endswith("warehouse.sqlite")
    assert body["db_exists"] is True


def test_health_reports_stub_mode_without_model(client: TestClient) -> None:
    body = client.get("/api/health").json()
    assert body["mode"] == "stub"
    assert body["model"] == ""          # stub 下不该编造一个模型名
    assert body["cache_enabled"] is True


# ---------------------------------------------------------------------------
# 同步问数
# ---------------------------------------------------------------------------
def test_ask_count_books(first_ask: dict) -> None:
    answer = first_ask["answer"]
    assert answer["status"] == "ok"
    assert answer["rowcount"] > 0
    assert answer["rows"]
    assert answer["sql"].upper().startswith("SELECT")
    assert answer["trace_id"].startswith("turn_")
    assert answer["mode"] == "stub"


def test_ask_returns_report_with_node_path(first_ask: dict) -> None:
    report = first_ask["report"]
    assert report["status"] == "ok"
    assert report["path"][0] == "intake"
    assert report["path"][-1] == "reporter"
    assert "sqlcraft" in report["path"] and "executor" in report["path"]
    assert report["elapsed_ms"] >= 0


def test_ask_rejects_empty_question(client: TestClient) -> None:
    res = client.post("/api/ask", json={"question": ""})
    assert res.status_code == 422
    assert "question" in json.dumps(res.json(), ensure_ascii=False)


def test_ask_rejects_blank_question(client: TestClient) -> None:
    res = client.post("/api/ask", json={"question": "   \n  "})
    assert res.status_code == 422


def test_ask_too_long_question_is_rejected_with_reason(client: TestClient) -> None:
    res = client.post("/api/ask", json={"question": "书" * 5000})
    assert res.status_code == 422
    text = json.dumps(res.json(), ensure_ascii=False)
    assert "过长" in text
    assert "2000" in text


def test_ask_accepts_history_and_session(client: TestClient) -> None:
    res = client.post("/api/ask", json={
        "question": "各分类的书籍数量",
        "session_id": "test-s-history",
        "history": [{"question": COUNT_QUESTION, "sql": "SELECT COUNT(*) FROM dim_book"}],
    })
    assert res.status_code == 200, res.text
    assert res.json()["answer"]["status"] == "ok"


# ---------------------------------------------------------------------------
# trace
# ---------------------------------------------------------------------------
def test_traces_list_after_ask(client: TestClient, first_ask: dict) -> None:
    res = client.get("/api/traces", params={"limit": 30})
    assert res.status_code == 200
    body = res.json()
    assert body["turns"], "跑过一次 ask 后 traces 不该为空"
    assert body["count"] == len(body["turns"])
    ids = [turn["turn_id"] for turn in body["turns"]]
    assert first_ask["answer"]["trace_id"] in ids
    assert body["turns"][0]["question"]


def test_trace_detail_returns_spans(client: TestClient, first_ask: dict) -> None:
    turn_id = first_ask["answer"]["trace_id"]
    res = client.get(f"/api/traces/{turn_id}")
    assert res.status_code == 200
    body = res.json()
    assert body["turn"]["turn_id"] == turn_id
    spans = body["spans"]
    assert spans, "spans 不能为空"
    assert [s["seq"] for s in spans] == sorted(s["seq"] for s in spans)
    nodes = [s["node"] for s in spans]
    assert nodes[0] == "intake" and "executor" in nodes
    for span in spans:
        assert isinstance(span["ms"], int)
        assert isinstance(span["meta"], dict)


def test_trace_unknown_id_returns_404(client: TestClient) -> None:
    res = client.get("/api/traces/turn_does_not_exist")
    assert res.status_code == 404
    assert "未找到" in res.json()["detail"]


def test_traces_limit_is_validated(client: TestClient) -> None:
    assert client.get("/api/traces", params={"limit": 0}).status_code == 422
    assert client.get("/api/traces", params={"limit": 999}).status_code == 422


# ---------------------------------------------------------------------------
# SSE
# ---------------------------------------------------------------------------
def test_stream_is_event_stream_and_ends_with_done(client: TestClient) -> None:
    with client.stream("POST", "/api/ask/stream", json={"question": "各分类的书籍数量"}) as res:
        assert res.status_code == 200
        assert res.headers["content-type"].startswith("text/event-stream")
        text = "".join(res.iter_text())

    assert "event: start" in text
    assert "event: done" in text
    events = _parse_sse(text)
    names = [name for name, _ in events]
    assert names[-1] == "done"
    done = events[-1][1]
    assert done["answer"]["status"] == "ok"
    assert done["report"]["path"]


def test_stream_pushes_real_node_events(client: TestClient) -> None:
    # 换一个没被前面的用例问过的问题，避免命中语义缓存后只剩一条 cache span
    text = client.post("/api/ask/stream", json={"question": "各出版社的书籍数量"}).text
    events = _parse_sse(text)
    nodes = [data for name, data in events if name == "node"]
    assert len(nodes) >= 6, "一次完整问数至少应该推 6 个节点事件"
    names = [event["node"] for event in nodes]
    assert names[0] == "intake"
    for event in nodes:
        assert event["turn_id"].startswith("turn_")
        assert isinstance(event["seq"], int)
        assert isinstance(event["ms"], int)
        assert event["status"]
    # 事件顺序必须与 trace 的 seq 一致
    assert [event["seq"] for event in nodes] == sorted(event["seq"] for event in nodes)


# ---------------------------------------------------------------------------
# 统计
# ---------------------------------------------------------------------------
def test_stats_fields_and_cache_hit_rate(client: TestClient, first_ask: dict) -> None:
    # 同一个问题再问一次，命中的应该是缓存
    again = client.post("/api/ask", json={"question": COUNT_QUESTION})
    assert again.status_code == 200
    assert again.json()["answer"]["cached"] is True

    res = client.get("/api/stats")
    assert res.status_code == 200
    body = res.json()
    for key in ("turns", "ok", "success_rate", "avg_elapsed_ms", "avg_steps", "spans", "cache", "mode"):
        assert key in body, key
    assert body["turns"] >= 2
    assert 0.0 <= body["success_rate"] <= 1.0
    cache = body["cache"]
    assert cache["hit_rate"] > 0
    assert cache["cached_turns"] >= 1
    assert cache["turns"] == body["turns"]


# ---------------------------------------------------------------------------
# 空结果：不能 500，也不能把空表当图表数据
# ---------------------------------------------------------------------------
def _empty_answer(question: str):
    from app.core.models import Answer, Insight, Verdict

    answer = Answer(
        question=question,
        status="ok",
        rowcount=0,
        columns=["日期", "评论数"],
        rows=[],
        insight=Insight(
            shape="empty",
            headline="该条件下没有查到数据",
            bullets=["建议放宽筛选条件"],
            footnotes=["结果 0 行"],
            chart=None,
        ),
        verdict=Verdict(),
        trace_id="turn_empty0001",
        mode="stub",
    )
    report = RunReport(
        path=["intake", "linker", "sqlcraft", "executor", "analyst", "critic", "reporter"],
        status="ok", steps=7, elapsed_ms=5,
    )
    return answer, report


def test_empty_result_does_not_500(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.api import server

    monkeypatch.setattr(server, "run_ask", lambda question, **_: _empty_answer(question))
    res = client.post("/api/ask", json={"question": "某个不存在的组合"})
    assert res.status_code == 200
    answer = res.json()["answer"]
    assert answer["rowcount"] == 0
    assert answer["rows"] == []
    assert answer["insight"]["chart"] is None
    assert answer["insight"]["shape"] == "empty"


def test_empty_result_stream_does_not_500(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.api import server

    monkeypatch.setattr(server, "run_ask", lambda question, **_: _empty_answer(question))
    res = client.post("/api/ask/stream", json={"question": "某个不存在的组合"})
    assert res.status_code == 200
    events = _parse_sse(res.text)
    assert events[-1][0] == "done"
    assert events[-1][1]["answer"]["rowcount"] == 0


# ---------------------------------------------------------------------------
# 前端:可离线、无外链
# ---------------------------------------------------------------------------
def test_index_is_served_and_has_no_external_refs(client: TestClient) -> None:
    res = client.get("/")
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("text/html")
    html = res.text
    assert "InsightPilot" in html
    assert "/static/app.js" in html and "/static/style.css" in html
    for bad in ("http://", "https://", "cdn.", "unpkg", "jsdelivr"):
        assert bad not in html, f"index.html 不应引用外部资源：{bad}"


def test_static_assets_are_offline_safe(client: TestClient) -> None:
    js = client.get("/static/app.js")
    assert js.status_code == 200
    assert "javascript" in js.headers["content-type"]
    css = client.get("/static/style.css")
    assert css.status_code == 200
    assert "text/css" in css.headers["content-type"]
    for asset in (js.text, css.text):
        for bad in ("http://", "https://", "cdn.", "unpkg", "jsdelivr", "echarts"):
            assert bad not in asset.lower(), f"静态资源不应外链：{bad}"


def test_static_rejects_unknown_and_traversal(client: TestClient) -> None:
    assert client.get("/static/missing.js").status_code == 404
    assert client.get("/static/..%2Fapp%2Fapi%2Fserver.py").status_code == 404
