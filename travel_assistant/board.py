from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field, replace
from typing import Any

TASK_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_]{1,40}$")

TODO, RUNNING, DONE, FAILED = "todo", "running", "done", "failed"


class BoardError(ValueError):
    pass


@dataclass(frozen=True)
class TaskCard:
    task_id: str
    title: str
    agent: str
    goal: str
    context: str
    depends_on: tuple[str, ...] = ()
    status: str = TODO
    result: str = ""
    attempts: int = 0
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["depends_on"] = list(self.depends_on)
        return data

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "TaskCard":
        return cls(
            task_id=str(raw["task_id"]),
            title=str(raw.get("title", "")),
            agent=str(raw.get("agent", "")),
            goal=str(raw.get("goal", "")),
            context=str(raw.get("context", "")),
            depends_on=tuple(str(d) for d in raw.get("depends_on", []) or []),
            status=str(raw.get("status", TODO)),
            result=str(raw.get("result", "")),
            attempts=int(raw.get("attempts", 0) or 0),
            error=str(raw.get("error", "")),
        )


@dataclass
class Board:
    cards: list[TaskCard] = field(default_factory=list)

    def get(self, task_id: str) -> TaskCard:
        for card in self.cards:
            if card.task_id == task_id:
                return card
        raise KeyError(task_id)

    def update(self, task_id: str, **changes: Any) -> TaskCard:
        for index, card in enumerate(self.cards):
            if card.task_id == task_id:
                self.cards[index] = replace(card, **changes)
                return self.cards[index]
        raise KeyError(task_id)

    def done_ids(self) -> set[str]:
        return {c.task_id for c in self.cards if c.status == DONE}

    def ready(self) -> list[TaskCard]:
        done = self.done_ids()
        return [c for c in self.cards if c.status == TODO and set(c.depends_on) <= done]

    def failed(self) -> list[TaskCard]:
        return [c for c in self.cards if c.status == FAILED]

    def blocked_by_failure(self) -> list[TaskCard]:
        """依赖链上游存在失败任务、永远无法执行的卡片。"""

        failed = {c.task_id for c in self.failed()}
        blocked: set[str] = set()
        changed = True
        while changed:
            changed = False
            for card in self.cards:
                if card.status == TODO and card.task_id not in blocked and set(card.depends_on) & (failed | blocked):
                    blocked.add(card.task_id)
                    changed = True
        return [c for c in self.cards if c.task_id in blocked]

    def is_complete(self) -> bool:
        return all(c.status == DONE for c in self.cards)

    def reset_unfinished(self) -> None:
        """断点续跑：running 视为中断，failed 重新给机会。"""

        for card in self.cards:
            if card.status in (RUNNING, FAILED):
                self.update(card.task_id, status=TODO, attempts=0, error="")

    def to_list(self) -> list[dict[str, Any]]:
        return [c.to_dict() for c in self.cards]

    @classmethod
    def from_list(cls, items: list[dict[str, Any]]) -> "Board":
        return cls([TaskCard.from_dict(item) for item in items])


def _text(item: dict[str, Any], key: str, label: str, limit: int) -> str:
    value = item.get(key)
    if not isinstance(value, str) or not value.strip():
        raise BoardError(f"{label}.{key} 必须是非空字符串")
    value = value.strip()
    return value if len(value) <= limit else value[:limit] + "…"


def validate_board(raw: Any, allowed_agents: set[str], min_tasks: int, max_tasks: int) -> Board:
    """主 Agent 输出的护栏：结构、数量、id、执行者、依赖必须合法且无环。"""

    items = raw.get("tasks") if isinstance(raw, dict) else None
    if not isinstance(items, list):
        raise BoardError("主 Agent 必须返回 {\"tasks\": [...]}")
    if not (min_tasks <= len(items) <= max_tasks):
        raise BoardError(f"任务数量必须在 {min_tasks}-{max_tasks} 之间，实际为 {len(items)}")

    cards: list[TaskCard] = []
    seen: set[str] = set()
    for index, item in enumerate(items, start=1):
        label = f"tasks[{index}]"
        if not isinstance(item, dict):
            raise BoardError(f"{label} 必须是对象")
        task_id = _text(item, "task_id", label, 60)
        if not TASK_ID_PATTERN.match(task_id):
            raise BoardError(f"{label}.task_id 必须是 lower_snake_case，实际为 {task_id!r}")
        if task_id in seen:
            raise BoardError(f"重复的 task_id: {task_id}")
        seen.add(task_id)

        agent = _text(item, "agent", label, 60)
        if agent not in allowed_agents:
            raise BoardError(f"{task_id}.agent 只能是 {sorted(allowed_agents)}，实际为 {agent!r}")

        deps = item.get("depends_on") or []
        if not isinstance(deps, list):
            raise BoardError(f"{task_id}.depends_on 必须是列表")

        cards.append(
            TaskCard(
                task_id=task_id,
                title=_text(item, "title", label, 80),
                agent=agent,
                goal=_text(item, "goal", label, 400),
                context=_text(item, "context", label, 800),
                depends_on=tuple(dict.fromkeys(str(d).strip() for d in deps if str(d).strip())),
            )
        )

    for card in cards:
        for dep in card.depends_on:
            if dep == card.task_id:
                raise BoardError(f"{card.task_id} 不能依赖自己")
            if dep not in seen:
                raise BoardError(f"{card.task_id} 依赖不存在的任务: {dep}")
    ensure_acyclic(cards)
    return Board(cards)


def ensure_acyclic(cards: list[TaskCard]) -> None:
    resolved: set[str] = set()
    pending = {c.task_id: c for c in cards}
    while pending:
        ready = [tid for tid, c in pending.items() if set(c.depends_on) <= resolved]
        if not ready:
            raise BoardError(f"任务依赖存在循环: {', '.join(sorted(pending))}")
        for tid in ready:
            resolved.add(tid)
            del pending[tid]


def execution_waves(board: Board) -> list[list[str]]:
    """按依赖分层，同一层的任务可以并行。"""

    waves: list[list[str]] = []
    resolved: set[str] = set()
    pending = {c.task_id: c for c in board.cards}
    while pending:
        wave = [tid for tid, c in pending.items() if set(c.depends_on) <= resolved]
        if not wave:
            break
        waves.append(wave)
        resolved.update(wave)
        for tid in wave:
            del pending[tid]
    return waves
