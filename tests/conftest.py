from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from travel_assistant.prompts import PLANNER_SYSTEM, SYNTH_SYSTEM

ROOT = Path(__file__).resolve().parents[1]

GOOD_PLAN = {
    "tasks": [
        {"task_id": "plan_route", "title": "路线", "agent": "route_planner", "goal": "定路线", "context": "30 天", "depends_on": []},
        {"task_id": "book_flights", "title": "机票", "agent": "flight_expert", "goal": "机票", "context": "上海出发", "depends_on": ["plan_route"]},
        {"task_id": "pick_hotels", "title": "酒店", "agent": "hotel_expert", "goal": "酒店", "context": "中档", "depends_on": ["plan_route"]},
        {"task_id": "food_guide", "title": "美食", "agent": "food_expert", "goal": "美食", "context": "地道", "depends_on": ["plan_route"]},
        {"task_id": "budget_review", "title": "预算", "agent": "budget_reviewer", "goal": "预算", "context": "25 万",
         "depends_on": ["book_flights", "pick_hotels", "food_guide"]},
    ]
}

SECTIONS = "\n".join(
    f"## {name}\n内容" for name in ("每日行程", "机票与城际交通", "住宿建议", "美食清单", "玩乐与门票", "教育与研学", "预算估算", "风险与待核实事项")
)


def make_report(days: int) -> str:
    daily = "\n".join(f"- 第 {d} 天：安排" for d in range(1, days + 1))
    return f"# 方案\n{SECTIONS}\n{daily}\n盐湖城到大峡谷约5小时"


class FakeLLM:
    """按 system prompt 区分主 Agent 建板 / 子 Agent / 汇总三种调用。"""

    def __init__(
        self,
        plans: list[Any] | None = None,
        fail_tasks: dict[str, int] | None = None,
        report_days: int = 30,
        delay: float = 0.0,
        fact_check_raises: bool = False,
        report_days_after_repair: int = 0,
    ) -> None:
        self.model = "fake"
        self.plans = list(plans or [GOOD_PLAN])
        self.fail_tasks = dict(fail_tasks or {})
        self.report_days = report_days
        self.report_days_after_repair = report_days_after_repair
        self.repair_prompts: list[str] = []
        self.delay = delay
        self.fact_check_raises = fact_check_raises
        self.agent_calls: list[str] = []
        self.worker_prompts: dict[str, str] = {}
        self.worker_systems: dict[str, str] = {}
        self.planner_prompts: list[str] = []
        self.active = 0
        self.max_active = 0
        self.closed = False

    async def complete(self, system: str, user: str, *, json_mode: bool = False, on_delta=None) -> str:
        if system == PLANNER_SYSTEM:
            self.planner_prompts.append(user)
            plan = self.plans.pop(0) if len(self.plans) > 1 else self.plans[0]
            return plan if isinstance(plan, str) else json.dumps(plan, ensure_ascii=False)
        if system == SYNTH_SYSTEM:
            if "缺少:" in user:
                self.repair_prompts.append(user)
                daily = "\n".join(f"| 第 {d} 天 | 城市 |" for d in range(1, self.report_days_after_repair + 1))
                return f"## 每日行程\n{daily}"
            return make_report(self.report_days)

        task_id = next(t["task_id"] for t in GOOD_PLAN["tasks"] if f"任务: {t['title']}\n" in user)
        self.worker_prompts[task_id] = user
        self.worker_systems[task_id] = system
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            await asyncio.sleep(self.delay)
            if self.fail_tasks.get(task_id, 0) > 0:
                self.fail_tasks[task_id] -= 1
                raise RuntimeError(f"boom {task_id}")
            text = f"## 结论\n{task_id} 的建议\n## 风险与待核实\n价格需核实\n## 交接摘要\nHANDOFF-{task_id}"
            if on_delta:
                for piece in (text[:10], text[10:]):
                    await on_delta(piece)
            return text
        finally:
            self.active -= 1

    async def aclose(self) -> None:
        self.closed = True

    async def run_agent(self, system, user, tools, *, max_rounds=6, on_delta=None, on_tool=None, require_tool=True):
        """模拟工具调用：子 Agent 查一次地点、一次路线；事实核查 Agent 修正一处错误。"""

        from travel_assistant.llm import AgentResult, ToolCallRecord
        from travel_assistant.prompts import FACT_CHECK_SYSTEM

        self.agent_calls.append(system)
        if system == FACT_CHECK_SYSTEM:
            if self.fact_check_raises:
                raise RuntimeError("fact check boom")
            outcome = await tools.call("drive_route", {"origin": "Salt Lake City", "destination": "Grand Canyon"})
            record = ToolCallRecord("drive_route", "{}", outcome.ok, outcome.content)
            if on_tool:
                await on_tool("tool_result", {"name": "drive_route", "arguments": "{}"}, record)
            verdict = {
                "checked": [
                    {"claim": "SLC 到大峡谷约 5 小时", "verdict": "wrong", "evidence": outcome.content},
                    {"claim": "某餐厅营业到午夜", "verdict": "unverified", "evidence": "查不到"},
                ],
                "corrections": [
                    {"find": "约5小时", "replace": "约10小时", "reason": "OSRM"},
                    {"find": "报告里不存在的文字", "replace": "x", "reason": "应被忽略"},
                ],
            }
            return AgentResult(json.dumps(verdict, ensure_ascii=False), [record], outcome.sources)

        place = await tools.call("lookup_place", {"name": "Katz's Delicatessen, New York"})
        route = await tools.call("drive_route", {"origin": "A", "destination": "B"})
        records = [
            ToolCallRecord("lookup_place", "{}", place.ok, place.content),
            ToolCallRecord("drive_route", "{}", route.ok, route.content),
        ]
        if on_tool:
            for record in records:
                await on_tool("tool_result", {"name": record.name, "arguments": "{}"}, record)
        text = await self.complete(system, user, on_delta=on_delta)
        return AgentResult(text, records, place.sources + route.sources)


class FakeBackend:
    """不联网的工具后端：固定的搜索结果、坐标和路线。"""

    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[tuple[str, Any]] = []
        self.closed = False

    async def search(self, query, max_results):
        from travel_assistant.tools import Source

        self.calls.append(("search", query))
        return [Source(f"Result for {query}", f"https://example.com/{len(self.calls)}", "snippet")]

    async def geocode(self, query):
        self.calls.append(("geocode", query))
        if self.fail or query == "Nowhere":
            return None
        return {"name": f"{query}, USA", "lat": 40.0 + len(query) / 100, "lon": -110.0, "osm_url": f"https://osm.org/{query}"}

    async def route(self, start, end):
        self.calls.append(("route", (start, end)))
        return {"km": 840.0, "hours": 10.2}

    async def aclose(self):
        self.closed = True


@pytest.fixture
def fake_factory():
    created: list[FakeLLM] = []

    def factory(**kwargs):
        def make(_model, _temperature):
            llm = FakeLLM(**kwargs)
            created.append(llm)
            return llm

        make.created = created
        return make

    return factory


@pytest.fixture
def scenario_path(tmp_path) -> Path:
    """真实的美国 30 天场景，仅把 min_tasks 放宽到假任务板的 5 张。"""

    data = json.loads((ROOT / "scenarios" / "usa_30days.json").read_text(encoding="utf-8"))
    data["min_tasks"] = 2
    path = tmp_path / "usa_30days.json"
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return path
