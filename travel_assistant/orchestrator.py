from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from travel_assistant.board import DONE, FAILED, RUNNING, TODO, Board, BoardError, validate_board
from travel_assistant.llm import LLM, ToolCallRecord, parse_json_object
from travel_assistant.prompts import (
    FACT_CHECK_SYSTEM,
    PLANNER_SYSTEM,
    SYNTH_SYSTEM,
    WORKER_SYSTEM,
    WORKER_SYSTEM_WITH_TOOLS,
    apply_corrections,
    fact_check_prompt,
    handoff_summary,
    missing_days,
    planner_prompt,
    repair_days_prompt,
    replace_section,
    sources_appendix,
    synth_prompt,
    worker_prompt,
)
from travel_assistant.scenario import TripRequest
from travel_assistant.tools import ToolBox

EventSink = Callable[[dict[str, Any]], Awaitable[None]]


async def _ignore(_event: dict[str, Any]) -> None:
    return None


@dataclass
class RunOptions:
    concurrency: int = 3
    max_attempts: int = 2
    plan_attempts: int = 3
    worker_max_chars: int = 900
    stream_workers: bool = True
    tool_rounds: int = 4
    fact_check: bool = True
    fact_check_claims: int = 12
    fact_check_rounds: int = 4


@dataclass
class RunState:
    """一次运行的完整状态；每次变化后都会通过 on_checkpoint 落盘，可断点续跑。"""

    request: TripRequest
    board: Board = field(default_factory=Board)
    raw_plan: dict[str, Any] = field(default_factory=dict)
    report: str = ""
    draft_report: str = ""
    fact_check: dict[str, Any] = field(default_factory=dict)
    status: str = "created"
    events: list[dict[str, Any]] = field(default_factory=list)
    worker_inputs: dict[str, str] = field(default_factory=dict)
    model: str = ""
    grounded: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "model": self.model,
            "grounded": self.grounded,
            "request": self.request.to_dict(),
            "raw_plan": self.raw_plan,
            "tasks": self.board.to_list(),
            "worker_inputs": self.worker_inputs,
            "draft_report": self.draft_report,
            "fact_check": self.fact_check,
            "report": self.report,
            "events": self.events,
        }


class Orchestrator:
    def __init__(
        self,
        planner: LLM,
        worker: LLM | None = None,
        options: RunOptions | None = None,
        on_event: EventSink | None = None,
        on_checkpoint: Callable[[RunState], None] | None = None,
        tools: ToolBox | None = None,
    ) -> None:
        self.planner = planner
        self.worker = worker or planner
        self.options = options or RunOptions()
        self.tools = tools
        self._sink = on_event or _ignore
        self._checkpoint = on_checkpoint or (lambda _state: None)
        self._lock = asyncio.Lock()

    async def emit(self, state: RunState, event: dict[str, Any]) -> None:
        event = {"ts": round(time.time(), 3), **event}
        if event.get("event") != "worker_delta":
            state.events.append(event)
        else:
            state.events.append({k: v for k, v in event.items() if k != "text"} | {"chars": len(event["text"])})
        await self._sink(event)

    def save(self, state: RunState) -> None:
        self._checkpoint(state)

    async def run(self, state: RunState) -> RunState:
        state.model = f"planner={self.planner.model}, worker={self.worker.model}"
        state.grounded = self.tools is not None
        if not state.board.cards:
            await self.plan(state)
        else:
            state.board.reset_unfinished()
            await self.emit(state, {"event": "resumed", "remaining": sum(c.status != DONE for c in state.board.cards)})
        await self.dispatch(state)
        if state.status == "dispatched":
            await self.synthesize(state)
        return state

    async def plan(self, state: RunState) -> None:
        state.status = "planning"
        await self.emit(state, {"event": "planning"})
        request = state.request
        error = ""
        for attempt in range(1, self.options.plan_attempts + 1):
            text = await self.planner.complete(PLANNER_SYSTEM, planner_prompt(request, error), json_mode=True)
            try:
                raw = parse_json_object(text)
                board = validate_board(raw, set(request.agents), request.min_tasks, request.max_tasks)
            except (ValueError, BoardError) as exc:
                error = str(exc)
                await self.emit(state, {"event": "plan_rejected", "attempt": attempt, "reason": error})
                continue
            state.raw_plan, state.board, state.status = raw, board, "planned"
            await self.emit(state, {"event": "board_created", "task_count": len(board.cards)})
            self.save(state)
            return
        state.status = "plan_failed"
        self.save(state)
        raise BoardError(f"主 Agent 连续 {self.options.plan_attempts} 次未能生成合法任务板: {error}")

    async def dispatch(self, state: RunState) -> None:
        state.status = "dispatching"
        board = state.board
        semaphore = asyncio.Semaphore(max(1, self.options.concurrency))
        running: dict[str, asyncio.Task[None]] = {}

        while True:
            for card in board.ready():
                if card.task_id not in running:
                    board.update(card.task_id, status=RUNNING, attempts=card.attempts + 1)
                    running[card.task_id] = asyncio.create_task(self._run_card(state, card.task_id, semaphore))
            if not running:
                break
            finished, _ = await asyncio.wait(running.values(), return_when=asyncio.FIRST_COMPLETED)
            for task_id in [tid for tid, task in running.items() if task in finished]:
                running.pop(task_id).result()

        skipped = board.blocked_by_failure()
        for card in skipped:
            await self.emit(state, {"event": "task_skipped", "task_id": card.task_id, "reason": "上游任务失败"})
        if board.is_complete():
            state.status = "dispatched"
            await self.emit(state, {"event": "dispatch_done"})
        else:
            state.status = "incomplete"
            await self.emit(
                state,
                {
                    "event": "dispatch_incomplete",
                    "failed": [c.task_id for c in board.failed()],
                    "skipped": [c.task_id for c in skipped],
                },
            )
        self.save(state)

    def _tool_callback(self, state: RunState, task_id: str):
        async def on_tool(_kind: str, call: dict[str, Any], record: ToolCallRecord) -> None:
            await self.emit(
                state,
                {
                    "event": "tool_call",
                    "task_id": task_id,
                    "tool": call["name"],
                    "arguments": call["arguments"][:200],
                    "ok": record.ok,
                    "summary": record.summary[:120],
                },
            )

        return on_tool

    async def _run_card(self, state: RunState, task_id: str, semaphore: asyncio.Semaphore) -> None:
        board, request = state.board, state.request
        async with semaphore:
            card = board.get(task_id)
            upstream = {dep: handoff_summary(board.get(dep).result) for dep in card.depends_on}
            prompt = worker_prompt(
                card, request.agents[card.agent], upstream, request.language, self.options.worker_max_chars
            )
            state.worker_inputs[task_id] = prompt
            await self.emit(
                state, {"event": "task_started", "task_id": task_id, "agent": card.agent, "attempt": card.attempts}
            )

            async def on_delta(text: str) -> None:
                await self.emit(state, {"event": "worker_delta", "task_id": task_id, "text": text})

            delta = on_delta if self.options.stream_workers else None
            sources: tuple[dict[str, str], ...] = ()
            tool_calls: tuple[dict[str, Any], ...] = ()
            try:
                if self.tools is not None:
                    agent_result = await self.worker.run_agent(
                        WORKER_SYSTEM_WITH_TOOLS,
                        prompt,
                        self.tools,
                        max_rounds=self.options.tool_rounds,
                        on_delta=delta,
                        on_tool=self._tool_callback(state, task_id),
                    )
                    result = agent_result.text
                    sources = tuple(s.to_dict() for s in agent_result.sources)
                    tool_calls = tuple(t.to_dict() for t in agent_result.tool_calls)
                else:
                    result = await self.worker.complete(WORKER_SYSTEM, prompt, on_delta=delta)
                if not result.strip():
                    raise RuntimeError("子 Agent 返回了空结果")
            except Exception as exc:
                retry = card.attempts < self.options.max_attempts
                async with self._lock:
                    board.update(task_id, status=TODO if retry else FAILED, error=str(exc)[:500])
                    self.save(state)
                await self.emit(
                    state,
                    {"event": "task_retry" if retry else "task_failed", "task_id": task_id, "reason": str(exc)[:200]},
                )
                return

            async with self._lock:
                board.update(
                    task_id, status=DONE, result=result.strip(), error="", sources=sources, tool_calls=tool_calls
                )
                self.save(state)
            await self.emit(
                state,
                {"event": "task_done", "task_id": task_id, "chars": len(result), "tools": len(tool_calls), "sources": len(sources)},
            )

    async def synthesize(self, state: RunState) -> None:
        state.status = "synthesizing"
        await self.emit(state, {"event": "synthesizing"})
        draft = (
            await self.planner.complete(
                SYNTH_SYSTEM, synth_prompt(state.request, state.board, grounded=self.tools is not None)
            )
        ).strip()
        draft = await self._repair_days(state, draft)
        state.draft_report = draft
        self.save(state)

        report = draft
        if self.tools is not None and self.options.fact_check:
            report = await self._fact_check(state, draft)
        state.report = report + "\n" + sources_appendix(state.board, state.fact_check or None)
        state.status = "completed"
        await self.emit(state, {"event": "report_ready", "chars": len(state.report)})
        self.save(state)

    async def _repair_days(self, state: RunState, draft: str) -> str:
        """主 Agent 偶尔会合并或省略天数；缺天时只重写「每日行程」章节，最多一次。"""

        days = state.request.days
        missing = missing_days(draft, days)
        if not missing:
            return draft
        await self.emit(state, {"event": "repairing_days", "missing": missing[:20]})
        section = await self.planner.complete(SYNTH_SYSTEM, repair_days_prompt(draft, days, missing))
        repaired = replace_section(draft, "## 每日行程", section)
        still = missing_days(repaired, days)
        await self.emit(state, {"event": "days_repaired", "before": len(missing), "after": len(still)})
        return repaired if len(still) < len(missing) else draft

    async def _fact_check(self, state: RunState, draft: str) -> str:
        """事实核查 Agent：拿着工具核查草稿中的硬事实，只应用逐字匹配、有证据的修正。"""

        assert self.tools is not None
        await self.emit(state, {"event": "fact_check_started"})
        try:
            agent_result = await self.planner.run_agent(
                FACT_CHECK_SYSTEM,
                fact_check_prompt(draft, self.options.fact_check_claims, state.request.language),
                self.tools,
                max_rounds=self.options.fact_check_rounds,
                on_tool=self._tool_callback(state, "fact_check"),
            )
            verdict = parse_json_object(agent_result.text)
        except Exception as exc:
            state.fact_check = {"error": str(exc)[:300], "checked": [], "applied": []}
            await self.emit(state, {"event": "fact_check_failed", "reason": str(exc)[:200]})
            return draft

        checked = [c for c in verdict.get("checked", []) if isinstance(c, dict)]
        corrections = [c for c in verdict.get("corrections", []) if isinstance(c, dict)]
        report, applied = apply_corrections(draft, corrections)
        state.fact_check = {
            "checked": checked,
            "proposed": corrections,
            "applied": applied,
            "tool_calls": [t.to_dict() for t in agent_result.tool_calls],
            "sources": [s.to_dict() for s in agent_result.sources],
        }
        await self.emit(
            state,
            {
                "event": "fact_check_done",
                "checked": len(checked),
                "wrong": sum(c.get("verdict") == "wrong" for c in checked),
                "applied": len(applied),
            },
        )
        return report


def state_from_dict(data: dict[str, Any]) -> RunState:
    from travel_assistant.scenario import request_from_dict

    return RunState(
        request=request_from_dict(data["request"]),
        board=Board.from_list(data.get("tasks", [])),
        raw_plan=data.get("raw_plan", {}),
        report=data.get("report", ""),
        draft_report=data.get("draft_report", ""),
        fact_check=dict(data.get("fact_check", {}) or {}),
        status=data.get("status", "created"),
        events=list(data.get("events", [])),
        worker_inputs=dict(data.get("worker_inputs", {})),
        model=data.get("model", ""),
        grounded=bool(data.get("grounded", False)),
    )


def dumps(state: RunState) -> str:
    return json.dumps(state.to_dict(), ensure_ascii=False, indent=2)
