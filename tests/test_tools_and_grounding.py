from __future__ import annotations

import asyncio

from travel_assistant.orchestrator import Orchestrator, RunOptions, RunState
from travel_assistant.prompts import (
    WORKER_SYSTEM,
    WORKER_SYSTEM_WITH_TOOLS,
    apply_corrections,
    missing_days,
    replace_section,
)
from travel_assistant.scenario import load_scenario
from travel_assistant.tools import ToolBox

from conftest import FakeBackend, FakeLLM


def run(coro):
    return asyncio.run(coro)


def test_toolbox_lookup_route_search_and_cache():
    backend = FakeBackend()
    box = ToolBox(backend)

    place = run(box.call("lookup_place", '{"name": "Katz\'s Delicatessen, New York"}'))
    assert place.ok and "Katz" in place.content and place.sources[0].url.startswith("https://osm.org/")

    route = run(box.call("drive_route", {"origin": "Salt Lake City", "destination": "Grand Canyon"}))
    assert route.ok and "840 公里" in route.content and "10.2 小时" in route.content
    assert "openstreetmap.org/directions" in route.sources[0].url

    again = run(box.call("drive_route", {"destination": "Grand Canyon", "origin": "Salt Lake City"}))
    assert again is route
    assert sum(1 for kind, _ in backend.calls if kind == "route") == 1, "相同参数应命中缓存"

    search = run(box.call("web_search", {"query": "Ahwahnee"}))
    assert search.ok and "[1]" in search.content and len(search.sources) == 1


def test_toolbox_errors_are_reported_to_model_not_raised():
    box = ToolBox(FakeBackend())
    assert not run(box.call("lookup_place", {"name": "Nowhere"})).ok
    assert "找不到地点" in run(box.call("drive_route", {"origin": "Nowhere", "destination": "B"})).content
    assert "未知工具" in run(box.call("teleport", {})).content
    assert "合法 JSON" in run(box.call("web_search", "{bad json")).content

    class Exploding(FakeBackend):
        async def search(self, query, max_results):
            raise TimeoutError("slow")

    result = run(ToolBox(Exploding()).call("web_search", {"query": "x"}))
    assert not result.ok and "TimeoutError" in result.content


def test_apply_corrections_only_exact_unique_matches():
    report = "第14天：盐湖城到大峡谷约5小时。入住 Ahwiyah Point Lodge。拉斯到大峡谷4h45m；大峡谷到盐湖城4h45m。"
    corrections = [
        {"find": "约5小时", "replace": "约10小时", "reason": "OSRM"},
        {"find": "Ahwiyah Point Lodge", "replace": "The Ahwahnee", "reason": "OSM"},
        {"find": "4h45m", "replace": "3h15m", "reason": "OSRM"},
        {"find": "大峡谷到盐湖城4h45m", "replace": "大峡谷到盐湖城约10h", "reason": "OSRM"},
        {"find": "不存在的原文", "replace": "x"},
        {"find": "", "replace": "x"},
    ]
    new, applied = apply_corrections(report, corrections)
    assert new == "第14天：盐湖城到大峡谷约10小时。入住 The Ahwahnee。拉斯到大峡谷4h45m；大峡谷到盐湖城约10h。"
    assert [a["find"] for a in applied] == ["约5小时", "Ahwiyah Point Lodge", "大峡谷到盐湖城4h45m"]
    assert "出现 2 次" in corrections[2]["skipped"]
    assert corrections[4]["skipped"] == "原文未出现在方案中"


def test_grounded_run_records_sources_and_fact_checks(scenario_path):
    llm = FakeLLM()
    events = []

    async def sink(e):
        events.append(e)

    tools = ToolBox(FakeBackend())
    state = run(Orchestrator(llm, tools=tools, on_event=sink).run(RunState(load_scenario(scenario_path))))

    assert state.status == "completed" and state.grounded
    assert all(c.sources and len(c.tool_calls) == 2 for c in state.board.cards)
    assert set(llm.worker_systems.values()) == {WORKER_SYSTEM_WITH_TOOLS}

    assert "约5小时" in state.draft_report
    assert "约10小时" in state.report and "约5小时" not in state.report.split("## 数据来源")[0]
    assert len(state.fact_check["applied"]) == 1, "不在报告中的修正必须被丢弃"
    assert "仍需人工确认" in state.report and "某餐厅营业到午夜" in state.report
    assert "未自动应用：原文未出现在方案中" in state.report

    kinds = [e["event"] for e in events]
    assert "tool_call" in kinds and "fact_check_done" in kinds
    assert kinds.index("fact_check_done") < kinds.index("report_ready")


def test_fact_check_failure_keeps_draft(scenario_path):
    llm = FakeLLM(fact_check_raises=True)
    state = run(Orchestrator(llm, tools=ToolBox(FakeBackend())).run(RunState(load_scenario(scenario_path))))
    assert state.status == "completed"
    assert "约5小时" in state.report
    assert "fact check boom" in state.fact_check["error"]


def test_offline_run_uses_plain_worker_prompt(scenario_path):
    llm = FakeLLM()
    state = run(Orchestrator(llm, options=RunOptions(fact_check=True)).run(RunState(load_scenario(scenario_path))))
    assert not state.grounded and state.fact_check == {}
    assert set(llm.worker_systems.values()) == {WORKER_SYSTEM}
    assert llm.agent_calls == []
    assert "离线模式" in state.report


def test_missing_days_detection():
    assert missing_days("第1天 第 2 天 Day 3 **D4**", 5) == [5]
    assert missing_days("| **第1天** | ... | **第26–30天** |", 3) == [2, 3]


def test_appendix_does_not_list_fixed_claims_as_unresolved():
    from travel_assistant.board import Board
    from travel_assistant.prompts import sources_appendix

    fact_check = {
        "checked": [
            {"claim": "自驾GC→LAS（4.5h，I-40）", "verdict": "wrong", "evidence": "3.2h"},
            {"claim": "Amtrak 2.5h", "verdict": "wrong", "evidence": "2h45m"},
        ],
        "applied": [{"find": "自驾GC→LAS（4.5h，I-40）[S10]", "replace": "自驾GC→LAS（3.2h，I-40）[S10]", "reason": "OSRM"}],
        "proposed": [],
    }
    text = sources_appendix(Board(), fact_check)
    manual = text.split("仍需人工确认:")[1]
    assert "Amtrak 2.5h" in manual and "GC→LAS" not in manual


def test_replace_section_keeps_other_sections():
    report = "# T\n## 每日行程\n旧\n## 预算估算\n钱"
    assert replace_section(report, "## 每日行程", "好的：\n## 每日行程\n新") == "# T\n## 每日行程\n新\n\n## 预算估算\n钱"
    assert replace_section(report, "## 每日行程", "没有标题") == report


def test_missing_days_are_repaired_once(scenario_path):
    llm = FakeLLM(report_days=25, report_days_after_repair=30)
    events = []

    async def sink(e):
        events.append(e)

    state = run(Orchestrator(llm, on_event=sink).run(RunState(load_scenario(scenario_path))))
    assert len(llm.repair_prompts) == 1 and "第 26 天" in llm.repair_prompts[0]
    assert missing_days(state.report, 30) == []
    assert "## 预算估算" in state.report, "其他章节必须保留"
    assert {"event": "days_repaired", "before": 5, "after": 0}.items() <= next(
        e for e in events if e["event"] == "days_repaired"
    ).items()


def test_failed_repair_keeps_original_draft(scenario_path):
    llm = FakeLLM(report_days=25, report_days_after_repair=3)
    state = run(Orchestrator(llm).run(RunState(load_scenario(scenario_path))))
    assert missing_days(state.report, 30) == list(range(26, 31))
