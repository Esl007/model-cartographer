"""Ask the Cartographer: routing, evidence selection, audit and limits.

Token Factory is never called: `ask._call` is replaced with a fake that
returns canned model text, so these tests are free and deterministic.
"""

from __future__ import annotations

import pytest

from cartographer.viewer import ask
from cartographer.viewer.ask import Fact, audit


def _fact(i: int, model: str, topics: set, text: str) -> Fact:
    return Fact(f"F{i}", model, frozenset(topics), text)


FACTS = [
    _fact(1, "Nemotron 3 Super 120B", {"overall"}, "Nemotron 3 Super 120B overall: 1180/1270 scored trials passed (93%)."),
    _fact(2, "Nemotron 3 Super 120B", {"tool_plan", "breaking"}, "breaks at 24-call plans; 8/28 scored passed."),
    _fact(3, "Nemotron 3 Nano 30B", {"tool_plan", "breaking"}, "breaks at 13-call plans; 5/21 scored passed."),
]


def test_audit_accepts_numbers_from_cited_facts_and_ignores_model_names():
    text = "Nemotron 3 Super 120B breaks at 24-call plans, 8/28 passed [F2]; Nano 30B breaks at 13 [F3]."
    a = audit(text, FACTS)
    assert a["unsupported"] == []
    assert {c["value"] for c in a["checks"]} == {"24", "8", "28", "13"}  # 3, 120B, 30B are names, not claims
    assert a["cited"] == ["F2", "F3"]


def test_audit_flags_a_number_that_is_not_in_the_evidence():
    a = audit("Super passes 97% of tool plans [F2].", FACTS)
    assert a["unsupported"] == ["97%"]


def test_audit_separates_cited_from_merely_present():
    a = audit("Super passed 93% overall [F2].", FACTS)  # 93% is in F1, but F2 was cited
    assert a["checks"] == [{"value": "93%", "status": "in evidence"}]


def test_select_filters_by_model_and_topic_and_falls_back(monkeypatch):
    monkeypatch.setattr(ask, "facts", lambda store: (FACTS, [], {}))
    assert [f.id for f in ask.select(None, ["Nemotron 3 Nano 30B"], ["tool_plan"])] == ["F3"]
    # a topic none of the chosen models has: keep their overview/breaking facts
    assert [f.id for f in ask.select(None, ["Nemotron 3 Super 120B"], ["code_repair"])] == ["F1", "F2"]


def test_plan_keeps_only_names_and_topics_from_the_allowed_lists(monkeypatch):
    monkeypatch.setattr(ask, "facts", lambda store: (FACTS, ["Nemotron 3 Super 120B", "Nemotron 3 Nano 30B"], {"tool_plan": "Tool plan"}))
    monkeypatch.setattr(ask, "_call", lambda *a, **k: {"text": '{"models": ["nemotron 3 super 120b", "GPT-9"], '
                                                               '"topics": ["tool_plan", "made_up"], "intent": "compare"}'})
    p = ask.plan(None, "How does Super do on tool plans?")
    assert p["models"] == ["Nemotron 3 Super 120B"] and p["topics"] == ["tool_plan"] and p["intent"] == "compare"


def test_answer_returns_text_evidence_and_audit(monkeypatch):
    monkeypatch.setattr(ask, "facts", lambda store: (FACTS, [], {}))
    monkeypatch.setattr(ask, "_call", lambda *a, **k: {"text": "Super breaks at 24-call plans [F2]."})
    out = ask.answer(None, "Where does Super break?", ["Nemotron 3 Super 120B"], ["breaking"])
    assert out["answer"].startswith("Super breaks") and out["audit"]["unsupported"] == []
    assert [f["id"] for f in out["facts"]] == ["F2"]


def test_limits_stop_a_visitor_after_the_per_ip_quota(monkeypatch):
    monkeypatch.setattr(ask, "PER_IP", 2)
    ask._HITS.clear()
    ask.check_limits("1.2.3.4")
    ask.check_limits("1.2.3.4")
    with pytest.raises(ask.AskUnavailable):
        ask.check_limits("1.2.3.4")
    ask.check_limits("5.6.7.8")  # another visitor is unaffected


def test_daily_budget_stops_every_visitor(monkeypatch):
    monkeypatch.setattr(ask, "DAILY_USD", 0.01)
    ask._HITS.clear()
    ask._SPEND.update(day="", usd=0.0)
    ask.check_limits("9.9.9.9")
    ask._charge(0.02)
    with pytest.raises(ask.AskUnavailable):
        ask.check_limits("8.8.8.8")
    ask._SPEND.update(day="", usd=0.0)
