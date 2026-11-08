from __future__ import annotations

import copy

import pytest

from travel_assistant.board import Board, BoardError, TaskCard, execution_waves, validate_board
from travel_assistant.llm import parse_json_object
from travel_assistant.prompts import handoff_summary
from travel_assistant.scenario import DEFAULT_AGENTS, apply_overrides, load_scenario, request_from_dict

from conftest import GOOD_PLAN

AGENTS = set(DEFAULT_AGENTS)


def plan_with(**changes):
    plan = copy.deepcopy(GOOD_PLAN)
    for index, patch in changes.items():
        plan["tasks"][int(index[1:])].update(patch)
    return plan


def test_valid_board_and_waves():
    board = validate_board(GOOD_PLAN, AGENTS, 2, 12)
    assert [c.task_id for c in board.cards][0] == "plan_route"
    assert execution_waves(board) == [
        ["plan_route"],
        ["book_flights", "pick_hotels", "food_guide"],
        ["budget_review"],
    ]


@pytest.mark.parametrize(
    "plan, message",
    [
        (plan_with(t0={"task_id": "Plan-Route"}), "lower_snake_case"),
        (plan_with(t1={"agent": "ghost"}), "agent 只能是"),
        (plan_with(t1={"depends_on": ["nope"]}), "不存在的任务"),
        (plan_with(t1={"depends_on": ["book_flights"]}), "不能依赖自己"),
        (plan_with(t0={"depends_on": ["budget_review"]}), "循环"),
        (plan_with(t2={"task_id": "book_flights"}), "重复"),
        (plan_with(t3={"goal": "  "}), "非空字符串"),
        ({"tasks": GOOD_PLAN["tasks"][:1]}, "任务数量"),
        ({"items": []}, "tasks"),
    ],
)
def test_invalid_boards_rejected(plan, message):
    with pytest.raises(BoardError, match=message):
        validate_board(plan, AGENTS, 2, 12)


def test_ready_and_blocked_by_failure():
    board = validate_board(GOOD_PLAN, AGENTS, 2, 12)
    assert [c.task_id for c in board.ready()] == ["plan_route"]
    board.update("plan_route", status="done")
    assert {c.task_id for c in board.ready()} == {"book_flights", "pick_hotels", "food_guide"}
    board.update("book_flights", status="failed")
    assert [c.task_id for c in board.blocked_by_failure()] == ["budget_review"]


def test_reset_unfinished_for_resume():
    board = Board([TaskCard("a", "A", "x", "g", "c", status="running", attempts=1),
                   TaskCard("b", "B", "x", "g", "c", status="failed", attempts=2, error="e"),
                   TaskCard("c", "C", "x", "g", "c", status="done", result="r")])
    board.reset_unfinished()
    assert [(c.status, c.attempts) for c in board.cards] == [("todo", 0), ("todo", 0), ("done", 0)]


def test_parse_json_object_tolerates_fences():
    assert parse_json_object('好的：\n```json\n{"tasks": []}\n```') == {"tasks": []}
    with pytest.raises(ValueError):
        parse_json_object("没有 JSON")


def test_handoff_summary_extracts_section():
    text = "## 结论\n很长的结论\n## 交接摘要\n关键信息\n## 附录\n不要"
    assert handoff_summary(text) == "关键信息"
    assert handoff_summary("没有小节的全文") == "没有小节的全文"


def test_scenario_file_and_overrides(scenario_path):
    request = load_scenario(scenario_path)
    assert request.days == 30 and len(request.must_see) >= 10
    changed = apply_overrides(request, {"days": 21, "interests": "美食，购物", "budget": None})
    assert changed.days == 21 and changed.interests == ["美食", "购物"]
    assert changed.budget == request.budget


def test_scenario_rejects_unknown_and_bad_values():
    with pytest.raises(ValueError, match="未知字段"):
        request_from_dict({"title": "t", "destination": "d", "days": 3, "dayz": 1})
    with pytest.raises(ValueError, match="days"):
        request_from_dict({"title": "t", "destination": "d", "days": 0})
