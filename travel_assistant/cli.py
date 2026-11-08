"""旅游规划助手命令行。

  python -m travel_assistant plan --scenario scenarios/usa_30days.json
  python -m travel_assistant plan --destination "日本 东京+京都" --days 7 --interests "美食,动漫"
  python -m travel_assistant list | show [run] | check [run] | resume [run] | init my_trip.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any, Sequence

from rich.console import Console

from travel_assistant.board import DONE
from travel_assistant.llm import LLM, LLMConfigError, QwenLLM
from travel_assistant.orchestrator import Orchestrator, RunOptions, RunState
from travel_assistant.render import LiveEventPrinter, print_report, print_summary
from travel_assistant.scenario import TripRequest, apply_overrides, load_scenario, scenario_template
from travel_assistant.storage import RunStore

LLMFactory = Any


def default_llm_factory(model: str | None, temperature: float) -> LLM:
    return QwenLLM(model=model, temperature=temperature)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="travel_assistant", description="多 Agent 旅游规划助手")
    parser.add_argument("--runs-dir", type=Path, help="运行记录目录（默认 .travel_runs 或 $TRAVEL_RUNS_DIR）")
    sub = parser.add_subparsers(dest="command", required=True)

    plan = sub.add_parser("plan", help="生成一份新的旅行方案")
    plan.add_argument("--scenario", type=Path, help="场景 JSON 文件；命令行参数会覆盖其中字段")
    plan.add_argument("--title")
    plan.add_argument("--destination")
    plan.add_argument("--days", type=int)
    plan.add_argument("--departure-city", dest="departure_city")
    plan.add_argument("--travelers")
    plan.add_argument("--budget")
    plan.add_argument("--travel-month", dest="travel_month")
    plan.add_argument("--interests", help="逗号分隔")
    plan.add_argument("--aspects", help="逗号分隔，例如 机票,酒店,美食")
    plan.add_argument("--must-see", dest="must_see", help="逗号分隔")
    plan.add_argument("--notes")
    plan.add_argument("--language")
    plan.add_argument("--max-tasks", dest="max_tasks", type=int)
    _add_run_options(plan)
    plan.add_argument("--dry-run", action="store_true", help="只生成并展示任务板，不执行子任务")

    resume = sub.add_parser("resume", help="继续一次未完成的运行（失败/中断的任务会重跑）")
    resume.add_argument("run", nargs="?", default="latest")
    _add_run_options(resume)

    sub.add_parser("list", help="列出历史运行")

    show = sub.add_parser("show", help="查看某次运行的任务板与最终方案")
    show.add_argument("run", nargs="?", default="latest")
    show.add_argument("--task", help="只看某张任务卡的子 Agent 输出")
    show.add_argument("--board-only", action="store_true")

    check = sub.add_parser("check", help="验收某次运行：任务全部完成、报告章节齐全")
    check.add_argument("run", nargs="?", default="latest")

    init = sub.add_parser("init", help="生成一个场景 JSON 模板")
    init.add_argument("path", type=Path)
    return parser


def _add_run_options(p: argparse.ArgumentParser) -> None:
    p.add_argument("--model", help="主 Agent 模型（默认 $QWEN_DEFAULT_MODEL 或 qwen-plus）")
    p.add_argument("--worker-model", help="子 Agent 模型（默认与主 Agent 相同）")
    p.add_argument("--concurrency", type=int, default=3, help="子 Agent 并发数")
    p.add_argument("--max-attempts", type=int, default=2, help="每张卡最多尝试次数")
    p.add_argument("--temperature", type=float, default=0.3)
    p.add_argument("-v", "--verbose", action="store_true", help="实时打印子 Agent 输出")


PLAN_FIELDS = (
    "title", "destination", "days", "departure_city", "travelers", "budget", "travel_month",
    "interests", "aspects", "must_see", "notes", "language", "max_tasks",
)


def build_request(args: argparse.Namespace) -> TripRequest:
    overrides = {name: getattr(args, name) for name in PLAN_FIELDS}
    if args.scenario:
        return apply_overrides(load_scenario(args.scenario), overrides)
    if not args.destination or not args.days:
        raise ValueError("没有 --scenario 时，至少需要 --destination 和 --days")
    overrides["title"] = overrides["title"] or f"{args.destination} {args.days} 天"
    base = TripRequest(title=overrides["title"], destination=args.destination, days=args.days)
    return apply_overrides(base, overrides)


async def _execute(
    args: argparse.Namespace,
    console: Console,
    store: RunStore,
    run_dir: Path,
    state: RunState,
    llm_factory: LLMFactory,
    dry_run: bool = False,
) -> int:
    planner = llm_factory(args.model, args.temperature)
    worker = llm_factory(args.worker_model, args.temperature) if args.worker_model else planner
    options = RunOptions(concurrency=args.concurrency, max_attempts=args.max_attempts)
    orch = Orchestrator(
        planner,
        worker,
        options,
        on_event=LiveEventPrinter(console, verbose=args.verbose),
        on_checkpoint=lambda s: store.save(run_dir, s),
    )
    console.print(f"[dim]运行目录: {run_dir}[/]")
    try:
        if dry_run:
            await orch.plan(state)
            print_summary(console, state)
            console.print(f"\n[dim]仅生成任务板。继续执行: python -m travel_assistant resume {run_dir.name}[/]")
            return 0
        await orch.run(state)
    except KeyboardInterrupt:
        store.save(run_dir, state)
        console.print(f"\n[yellow]已中断，进度已保存。继续: python -m travel_assistant resume {run_dir.name}[/]")
        return 130
    finally:
        await planner.aclose()
        if worker is not planner:
            await worker.aclose()

    console.print()
    print_summary(console, state)
    if state.status != "completed":
        console.print(f"[red]运行未完成（{state.status}）。修复后可执行: python -m travel_assistant resume {run_dir.name}[/]")
        return 1
    console.print(f"\n[green]最终方案:[/] {run_dir / 'report.md'}")
    console.print(f"[dim]查看: python -m travel_assistant show {run_dir.name}[/]")
    return 0


def cmd_plan(args: argparse.Namespace, console: Console, store: RunStore, llm_factory: LLMFactory) -> int:
    request = build_request(args)
    run_dir = store.new_run_dir(request.title)
    state = RunState(request=request)
    store.save(run_dir, state)
    return asyncio.run(_execute(args, console, store, run_dir, state, llm_factory, dry_run=args.dry_run))


def cmd_resume(args: argparse.Namespace, console: Console, store: RunStore, llm_factory: LLMFactory) -> int:
    run_dir = store.resolve(args.run)
    state = store.load(run_dir)
    if state.status == "completed":
        console.print(f"[green]{run_dir.name} 已经完成，无需续跑。[/]")
        return 0
    return asyncio.run(_execute(args, console, store, run_dir, state, llm_factory))


def cmd_list(console: Console, store: RunStore) -> int:
    from rich.table import Table

    runs = store.list_runs()
    if not runs:
        console.print(f"[dim]{store.root} 下还没有运行记录[/]")
        return 0
    table = Table(title=f"运行记录 ({store.root})")
    for col in ("运行", "标题", "天数", "状态", "任务进度"):
        table.add_column(col)
    for run_dir in runs:
        try:
            state = store.load(run_dir)
        except Exception as exc:
            table.add_row(run_dir.name, f"[red]无法读取: {exc}[/]", "", "", "")
            continue
        done = sum(c.status == DONE for c in state.board.cards)
        table.add_row(
            run_dir.name, state.request.title, str(state.request.days), state.status,
            f"{done}/{len(state.board.cards)}",
        )
    console.print(table)
    return 0


def cmd_show(args: argparse.Namespace, console: Console, store: RunStore) -> int:
    run_dir = store.resolve(args.run)
    state = store.load(run_dir)
    if args.task:
        card = state.board.get(args.task)
        console.rule(f"{card.task_id} · {card.agent} · {card.status}")
        print_report(console, card.result or f"_（无输出）_ {card.error}")
        return 0
    print_summary(console, state)
    if not args.board_only:
        console.rule("最终方案")
        print_report(console, state.report or "_尚未生成最终方案_")
    return 0


REQUIRED_SECTIONS = ("每日行程", "机票", "住宿", "美食", "玩乐", "教育", "预算", "风险")


def check_state(state: RunState) -> list[str]:
    """返回所有验收问题；空列表表示通过。"""

    problems: list[str] = []
    if not state.board.cards:
        problems.append("任务板为空")
    for card in state.board.cards:
        if card.status != DONE:
            problems.append(f"任务未完成: {card.task_id} ({card.status})")
        elif not card.result.strip():
            problems.append(f"任务结果为空: {card.task_id}")
    worker_events = [e for e in state.events if e.get("event") == "worker_delta"]
    if state.board.cards and not worker_events:
        problems.append("没有观察到子 Agent 流式输出 (worker_delta)")
    if not state.report.strip():
        problems.append("最终方案为空")
    else:
        for section in REQUIRED_SECTIONS:
            if section not in state.report:
                problems.append(f"最终方案缺少章节: {section}")
        days = state.request.days
        if f"第 {days} 天" not in state.report and f"第{days}天" not in state.report and f"Day {days}" not in state.report:
            problems.append(f"每日行程没有覆盖到第 {days} 天")
    return problems


def cmd_check(args: argparse.Namespace, console: Console, store: RunStore) -> int:
    run_dir = store.resolve(args.run)
    state = store.load(run_dir)
    print_summary(console, state)
    problems = check_state(state)
    deltas = sum(e.get("event") == "worker_delta" for e in state.events)
    console.print(f"worker_delta 事件: {deltas}")
    if problems:
        console.print("[red]验收未通过:[/]")
        for item in problems:
            console.print(f"  [red]✗[/] {item}")
        return 1
    console.print("[green]✓ 验收通过[/]")
    return 0


def cmd_init(args: argparse.Namespace, console: Console) -> int:
    if args.path.exists():
        console.print(f"[red]{args.path} 已存在，不覆盖[/]")
        return 1
    args.path.parent.mkdir(parents=True, exist_ok=True)
    args.path.write_text(json.dumps(scenario_template(), ensure_ascii=False, indent=2), encoding="utf-8")
    console.print(f"[green]已生成场景模板:[/] {args.path}")
    console.print(f"[dim]编辑后运行: python -m travel_assistant plan --scenario {args.path}[/]")
    return 0


def main(
    argv: Sequence[str] | None = None,
    *,
    llm_factory: LLMFactory = default_llm_factory,
    console: Console | None = None,
) -> int:
    try:
        from dotenv import find_dotenv, load_dotenv

        load_dotenv(find_dotenv(usecwd=True))
    except ImportError:
        pass
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except (OSError, ValueError):
            pass

    args = build_parser().parse_args(argv)
    console = console or Console()
    store = RunStore(args.runs_dir.resolve() if args.runs_dir else None)
    try:
        if args.command == "plan":
            return cmd_plan(args, console, store, llm_factory)
        if args.command == "resume":
            return cmd_resume(args, console, store, llm_factory)
        if args.command == "list":
            return cmd_list(console, store)
        if args.command == "show":
            return cmd_show(args, console, store)
        if args.command == "check":
            return cmd_check(args, console, store)
        if args.command == "init":
            return cmd_init(args, console)
    except (LLMConfigError, FileNotFoundError, ValueError, KeyError) as exc:
        console.print(f"[red]错误:[/] {exc}")
        return 2
    return 2
