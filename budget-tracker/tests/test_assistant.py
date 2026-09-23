"""The assistant's loop and tools, with a fake Claude (no API calls, no cost)."""

from types import SimpleNamespace as NS

import pytest

from app import assistant
from app.db import get_conn
from tests.test_import import upload

# client fixture comes from test_import
from tests.test_import import client  # noqa: F401


class FakeClaude:
    """Plays back scripted responses and records what it was sent."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.messages = self

    def create(self, **kwargs):
        # snapshot: the loop appends to the same list after we return
        self.calls.append({**kwargs, "messages": list(kwargs["messages"])})
        return self.responses.pop(0)


def tool_call(name, args, id="t1"):
    return NS(stop_reason="tool_use", content=[NS(type="tool_use", id=id, name=name, input=args)])


def answer(text):
    return NS(stop_reason="end_turn", content=[NS(type="text", text=text)])


def test_tools_on_real_rows(client):  # noqa: F811
    upload(client)
    with get_conn() as conn:
        amazon = assistant.tool_query_transactions(conn, {"start_month": "2026-04", "search": "amzn"})
        assert amazon["match_count"] == 1 and amazon["rows"][0]["category"] == "Shopping"
        food = assistant.tool_top_merchants(conn, {"start_month": "2026-04", "end_month": "2026-04",
                                                   "category": "Food & Dining"})
        assert food["merchants"][0]["merchant"] == "DLO RAPPI"
        assert food["merchants"][0]["total"] == round(324.11 - 16.15, 2)  # refund nets out
        uncat = assistant.tool_query_transactions(conn, {"category": "Uncategorized"})
        assert {r["description"] for r in uncat["rows"]} == {"SOME NEW PLACE", "JUAN VALDEZ ARP INTERN"}
        # one-offs are excluded from merchant totals unless asked for
        assert all(m["merchant"] != "PUBLIC STORAGE"
                   for m in assistant.tool_top_merchants(conn, {"start_month": "2026-01", "end_month": "2026-12"})["merchants"])


def test_bad_tool_input_goes_back_to_claude_as_error(client):  # noqa: F811
    with get_conn() as conn:
        text, is_error = assistant.run_tool(conn, "set_merchant_category", {"pattern": "X", "category": "Nope"})
    assert is_error and "category must be one of" in text


def test_loop_runs_tool_then_answers(client):  # noqa: F811
    upload(client)
    fake = FakeClaude([
        tool_call("top_merchants", {"start_month": "2026-04", "end_month": "2026-04", "category": "Food & Dining"}),
        answer("Food was $380, mostly **Rappi**."),
    ])
    with get_conn() as conn:
        result = assistant.ask(conn, [], "why is food so high?", client=fake)
    assert result == {"reply": "Food was $380, mostly **Rappi**.", "tools_used": ["top_merchants"]}
    first, second = fake.calls
    assert first["model"] == "claude-sonnet-4-6"
    assert {t["name"] for t in first["tools"]} == {"query_transactions", "top_merchants", "set_merchant_category"}
    assert '"months"' in first["system"][1]["text"]  # budget snapshot is included
    tool_result = second["messages"][-1]["content"][0]
    assert tool_result["type"] == "tool_result" and "DLO RAPPI" in tool_result["content"]


def test_correction_saves_rule(client):  # noqa: F811
    upload(client)
    fake = FakeClaude([
        tool_call("set_merchant_category", {"pattern": "SOME NEW PLACE", "category": "Food & Dining"}),
        answer("Done: SOME NEW PLACE is now Food & Dining."),
    ])
    with get_conn() as conn:
        assistant.ask(conn, [], "some new place is a restaurant", client=fake)
    txs = {t["description"]: t for t in client.get("/transactions?month=2026-04").json()}
    assert txs["SOME NEW PLACE"]["category"] == "Food & Dining"


def test_chat_endpoint_stores_history_per_user(client, monkeypatch):  # noqa: F811
    monkeypatch.setenv("APP_USERS", "Mariano:a,Maura:b")
    fake = FakeClaude([answer("Hi Maura"), answer("Second answer")])
    monkeypatch.setattr(assistant, "_client", lambda: fake)
    assert client.post("/chat", json={"message": "hello"}, auth=("Maura", "b")).json()["reply"] == "Hi Maura"
    client.post("/chat", json={"message": "and now?"}, auth=("Maura", "b"))
    # the second question was sent with the first exchange as context
    assert [m["role"] for m in fake.calls[1]["messages"]] == ["user", "assistant", "user"]
    assert len(client.get("/chat", auth=("Maura", "b")).json()) == 4
    assert client.get("/chat", auth=("Mariano", "a")).json() == []  # separate threads
    client.delete("/chat", auth=("Maura", "b"))
    assert client.get("/chat", auth=("Maura", "b")).json() == []


def test_chat_without_api_key_explains(client, monkeypatch):  # noqa: F811
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    r = client.post("/chat", json={"message": "hello"})
    assert r.status_code == 503 and "ANTHROPIC_API_KEY" in r.json()["detail"]


def test_rolling_average_skips_partial_months():
    table = [
        {"month": "2026-01", "variable_total": 100, "categories": {"Travel": 100}, "data_from": "2026-01-23", "data_to": "2026-01-31"},
        {"month": "2026-02", "variable_total": 300, "categories": {"Travel": 300}, "data_from": "2026-02-01", "data_to": "2026-02-28"},
        {"month": "2026-03", "variable_total": 600, "categories": {"Travel": 600}, "data_from": "2026-03-02", "data_to": "2026-03-30"},
        {"month": "2026-04", "variable_total": 50, "categories": {"Travel": 50}, "data_from": "2026-04-01", "data_to": "2026-04-10"},
    ]
    avg = assistant.rolling_averages(table)
    assert avg["months"] == ["2026-02", "2026-03"]
    assert avg["variable_total"] == 450


def test_chat_hidden_without_key(client, monkeypatch):  # noqa: F811
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert client.get("/config").json()["assistant_enabled"] is False
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    assert client.get("/config").json()["assistant_enabled"] is True
