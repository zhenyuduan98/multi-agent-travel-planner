from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from travel_assistant.board import DONE, FAILED, RUNNING, TODO, Board, BoardError, validate_board
from travel_assistant.llm import LLM, parse_json_object
from travel_assistant.prompts import (
    PLANNER_SYSTEM,
    SYNTH_SYSTEM,
    WORKER_SYSTEM,
    handoff_summary,
    planner_prompt,
    synth_prompt,
    worker_prompt,
)
from travel_assistant.scenario import TripRequest

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


@dataclass
class RunState:
    """一次运行的完整状态；每次变化后都会通过 on_checkpoint 落盘，可断点续跑。"""

    request: TripRequest
    board: Board = field(default_factory=Board)
    raw_plan: dict[str, Any] = field(default_factory=dict)
    report: str = ""
    status: str = "created"
    events: list[dict[str, Any]] = field(default_factory=list)
    worker_inputs: dict[str, str] = field(default_factory=dict)
    model: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "model": self.model,
            "request": self.request.to_dict(),
            "raw_plan": self.raw_plan,
            "tasks": self.board.to_list(),
            "worker_inputs": self.worker_inputs,
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
    ) -> None:
        self.planner = planner
        self.worker = worker or planner
        self.options = options or RunOptions()
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

            try:
                result = await self.worker.complete(
                    WORKER_SYSTEM, prompt, on_delta=on_delta if self.options.stream_workers else None
                )
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
                board.update(task_id, status=DONE, result=result.strip(), error="")
                self.save(state)
            await self.emit(state, {"event": "task_done", "task_id": task_id, "chars": len(result)})

    async def synthesize(self, state: RunState) -> None:
        state.status = "synthesizing"
        await self.emit(state, {"event": "synthesizing"})
        state.report = (await self.planner.complete(SYNTH_SYSTEM, synth_prompt(state.request, state.board))).strip()
        state.status = "completed"
        await self.emit(state, {"event": "report_ready", "chars": len(state.report)})
        self.save(state)


def state_from_dict(data: dict[str, Any]) -> RunState:
    from travel_assistant.scenario import request_from_dict

    return RunState(
        request=request_from_dict(data["request"]),
        board=Board.from_list(data.get("tasks", [])),
        raw_plan=data.get("raw_plan", {}),
        report=data.get("report", ""),
        status=data.get("status", "created"),
        events=list(data.get("events", [])),
        worker_inputs=dict(data.get("worker_inputs", {})),
        model=data.get("model", ""),
    )


def dumps(state: RunState) -> str:
    return json.dumps(state.to_dict(), ensure_ascii=False, indent=2)
