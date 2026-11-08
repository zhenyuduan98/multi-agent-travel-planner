from __future__ import annotations

from typing import Any

from rich.console import Console
from rich.markdown import Markdown
from rich.markup import escape
from rich.panel import Panel
from rich.table import Table

from travel_assistant.board import DONE, FAILED, RUNNING, Board, execution_waves
from travel_assistant.orchestrator import RunState

STATUS_STYLE = {DONE: "green", RUNNING: "yellow", FAILED: "red"}


def board_table(board: Board, title: str = "任务板") -> Table:
    table = Table(title=title, show_lines=False, expand=True)
    table.add_column("#", justify="right", style="dim", width=3)
    table.add_column("task_id", style="cyan", overflow="fold")
    table.add_column("子 Agent", style="magenta", overflow="fold")
    table.add_column("标题", ratio=3, overflow="fold")
    table.add_column("依赖", style="dim", ratio=2, overflow="fold")
    table.add_column("状态", no_wrap=True)
    for index, card in enumerate(board.cards, start=1):
        style = STATUS_STYLE.get(card.status, "white")
        table.add_row(
            str(index),
            card.task_id,
            card.agent,
            escape(card.title),
            "\n".join(card.depends_on) or "-",
            f"[{style}]{card.status}[/{style}]",
        )
    return table


def waves_text(board: Board) -> str:
    return escape("  →  ".join(f"[{' | '.join(wave)}]" for wave in execution_waves(board)))


class LiveEventPrinter:
    """把编排器事件打印到终端。默认只显示进度；verbose 时实时打印子 Agent 输出。"""

    def __init__(self, console: Console, verbose: bool = False) -> None:
        self.console = console
        self.verbose = verbose
        self.chars: dict[str, int] = {}

    async def __call__(self, event: dict[str, Any]) -> None:
        kind = event.get("event")
        task_id = event.get("task_id", "")
        c = self.console
        if kind == "worker_delta":
            self.chars[task_id] = self.chars.get(task_id, 0) + len(event["text"])
            if self.verbose:
                c.print(event["text"], end="", style="dim", markup=False, highlight=False)
            return
        event = {k: escape(v) if isinstance(v, str) else v for k, v in event.items()}
        if kind == "planning":
            c.print("[bold]主 Agent[/] 正在拆解任务板…")
        elif kind == "plan_rejected":
            c.print(f"[yellow]任务板第 {event['attempt']} 次未通过校验，重新生成:[/] {event['reason']}")
        elif kind == "board_created":
            c.print(f"[green]✓[/] 任务板已生成，共 {event['task_count']} 张卡")
        elif kind == "resumed":
            c.print(f"[cyan]↻[/] 断点续跑，剩余 {event['remaining']} 张卡")
        elif kind == "task_started":
            attempt = f"（第 {event['attempt']} 次）" if event.get("attempt", 1) > 1 else ""
            c.print(f"[yellow]▶[/] {task_id} [magenta]{event['agent']}[/] 开始{attempt}")
        elif kind == "task_done":
            c.print(f"{'' if not self.verbose else chr(10)}[green]✓[/] {task_id} 完成（{event['chars']} 字）")
        elif kind == "task_retry":
            c.print(f"[yellow]↻[/] {task_id} 失败，将重试: {event['reason']}")
        elif kind == "task_failed":
            c.print(f"[red]✗[/] {task_id} 失败: {event['reason']}")
        elif kind == "task_skipped":
            c.print(f"[red]-[/] {task_id} 跳过: {event['reason']}")
        elif kind == "dispatch_done":
            c.print("[green]✓[/] 所有子任务完成")
        elif kind == "dispatch_incomplete":
            c.print(f"[red]任务未全部完成[/] failed={event['failed']} skipped={event['skipped']}")
        elif kind == "synthesizing":
            c.print("[bold]主 Agent[/] 正在汇总最终方案…")
        elif kind == "report_ready":
            c.print(f"[green]✓[/] 最终方案已生成（{event['chars']} 字）")


def print_summary(console: Console, state: RunState) -> None:
    req = state.request
    console.print(
        Panel.fit(
            escape(
                f"{req.title}\n{req.destination} · {req.days} 天 · {req.travelers} · 预算 {req.budget}\n"
                f"状态: {state.status}   模型: {state.model or '-'}"
            ),
            title="运行概览",
        )
    )
    if state.board.cards:
        console.print(board_table(state.board))
        console.print(f"[dim]执行波次: {waves_text(state.board)}[/]")


def print_report(console: Console, report: str) -> None:
    console.print(Markdown(report))
