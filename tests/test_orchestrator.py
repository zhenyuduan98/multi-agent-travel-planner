from __future__ import annotations

import asyncio

import pytest

from travel_assistant.board import BoardError
from travel_assistant.orchestrator import Orchestrator, RunOptions, RunState
from travel_assistant.scenario import load_scenario

from conftest import GOOD_PLAN, FakeLLM


def run(orch: Orchestrator, state: RunState) -> RunState:
    return asyncio.run(orch.run(state))


def test_full_run_parallel_and_context_isolation(scenario_path):
    llm = FakeLLM(delay=0.05)
    events: list[dict] = []

    async def sink(event):
        events.append(event)

    state = run(Orchestrator(llm, options=RunOptions(concurrency=3), on_event=sink), RunState(load_scenario(scenario_path)))

    assert state.status == "completed"
    assert state.board.is_complete()
    assert "第 30 天" in state.report
    assert llm.max_active == 3, "中间一层三张卡应并行执行"

    budget_prompt = llm.worker_prompts["budget_review"]
    for dep in ("book_flights", "pick_hotels", "food_guide"):
        assert f"HANDOFF-{dep}" in budget_prompt
    assert "HANDOFF-plan_route" not in budget_prompt, "非直接上游不应进入私有任务包"
    assert "plan_route 的建议" not in budget_prompt, "下游只拿交接摘要，不拿全文"
    assert "必去" not in llm.worker_prompts["plan_route"], "子 Agent 看不到用户完整需求"

    kinds = [e["event"] for e in events]
    assert kinds[0] == "planning" and kinds[-1] == "report_ready"
    assert kinds.count("task_done") == 5 and "worker_delta" in kinds


def test_concurrency_limit_respected(scenario_path):
    llm = FakeLLM(delay=0.05)
    run(Orchestrator(llm, options=RunOptions(concurrency=1)), RunState(load_scenario(scenario_path)))
    assert llm.max_active == 1


def test_invalid_plan_is_regenerated_with_feedback(scenario_path):
    bad = {"tasks": [{**t, "agent": "ghost"} for t in GOOD_PLAN["tasks"]]}
    llm = FakeLLM(plans=["不是 JSON", bad, GOOD_PLAN])
    events = []

    async def sink(e):
        events.append(e)

    state = run(Orchestrator(llm, on_event=sink), RunState(load_scenario(scenario_path)))
    assert state.status == "completed"
    assert [e["event"] for e in events].count("plan_rejected") == 2
    assert "上一次的输出没有通过校验" in llm.planner_prompts[-1]


def test_plan_gives_up_after_max_attempts(scenario_path):
    llm = FakeLLM(plans=["{}"])
    state = RunState(load_scenario(scenario_path))
    with pytest.raises(BoardError, match="连续 3 次"):
        run(Orchestrator(llm), state)
    assert state.status == "plan_failed"


def test_retry_then_success(scenario_path):
    llm = FakeLLM(fail_tasks={"pick_hotels": 1})
    state = run(Orchestrator(llm, options=RunOptions(max_attempts=2)), RunState(load_scenario(scenario_path)))
    assert state.status == "completed"
    assert state.board.get("pick_hotels").attempts == 2


def test_failure_skips_downstream_but_keeps_siblings(scenario_path):
    llm = FakeLLM(fail_tasks={"pick_hotels": 5})
    state = run(Orchestrator(llm, options=RunOptions(max_attempts=2)), RunState(load_scenario(scenario_path)))
    assert state.status == "incomplete"
    assert state.report == ""
    statuses = {c.task_id: c.status for c in state.board.cards}
    assert statuses == {
        "plan_route": "done", "book_flights": "done", "pick_hotels": "failed",
        "food_guide": "done", "budget_review": "todo",
    }


def test_resume_only_reruns_unfinished(scenario_path):
    state = run(Orchestrator(FakeLLM(fail_tasks={"pick_hotels": 5})), RunState(load_scenario(scenario_path)))
    assert state.status == "incomplete"

    second = FakeLLM()
    state = run(Orchestrator(second), state)
    assert state.status == "completed"
    assert set(second.worker_prompts) == {"pick_hotels", "budget_review"}
    assert second.planner_prompts == [], "续跑不应重新建板"


def test_checkpoints_written_during_run(scenario_path):
    snapshots: list[str] = []
    run(
        Orchestrator(FakeLLM(), on_checkpoint=lambda s: snapshots.append(s.status)),
        RunState(load_scenario(scenario_path)),
    )
    assert snapshots[0] == "planned" and snapshots[-1] == "completed"
    assert len(snapshots) >= 7
