from __future__ import annotations

import io
import json

from rich.console import Console

from travel_assistant.cli import main
from travel_assistant.tools import ToolBox

from conftest import FakeBackend


def fake_tools_factory():
    return ToolBox(FakeBackend())


def cli(argv, factory=None, tmp_path=None, tools_factory=fake_tools_factory):
    buffer = io.StringIO()
    console = Console(file=buffer, width=160, force_terminal=False, color_system=None)
    args = ["--runs-dir", str(tmp_path / "runs"), *argv]
    kwargs = {"console": console, "tools_factory": tools_factory}
    if factory is not None:
        kwargs["llm_factory"] = factory
    code = main(args, **kwargs)
    return code, buffer.getvalue()


def test_plan_list_show_check(tmp_path, fake_factory, scenario_path):
    factory = fake_factory()
    code, out = cli(["plan", "--scenario", str(scenario_path), "--concurrency", "2"], factory, tmp_path)
    assert code == 0, out
    assert "最终方案已生成" in out
    assert factory.created and factory.created[0].closed

    run_dir = next((tmp_path / "runs").iterdir())
    assert (run_dir / "report.md").read_text(encoding="utf-8").startswith("# 方案")
    saved = json.loads((run_dir / "state.json").read_text(encoding="utf-8"))
    assert saved["status"] == "completed" and len(saved["tasks"]) == 5

    code, out = cli(["list"], tmp_path=tmp_path)
    assert code == 0 and "completed" in out and "5/5" in out

    code, out = cli(["show", "latest", "--task", "budget_review"], tmp_path=tmp_path)
    assert code == 0 and "budget_review 的建议" in out

    code, out = cli(["check"], tmp_path=tmp_path)
    assert code == 0 and "验收通过" in out
    assert "联网核实: 工具调用 10 次" in out and "自动修正 1 处" in out

    report = (run_dir / "report.md").read_text(encoding="utf-8")
    body, appendix = report.split("## 数据来源与事实核查")
    assert "约10小时" in body and "约5小时" not in body
    assert "| 约5小时 | 约10小时 | OSRM |" in appendix and "https://osm.org/" in appendix

    code, out = cli(["show", "latest", "--task", "plan_route"], tmp_path=tmp_path)
    assert "工具调用（2 次）" in out and "来源" in out


def test_offline_flag_skips_tools(tmp_path, fake_factory, scenario_path):
    def boom():
        raise AssertionError("离线模式不应创建工具")

    code, out = cli(["plan", "--scenario", str(scenario_path), "--offline"], fake_factory(), tmp_path, tools_factory=boom)
    assert code == 0 and "离线" in out
    code, out = cli(["check"], tmp_path=tmp_path, tools_factory=boom)
    assert code == 0 and "内容未经联网核实" in out
    report = next((tmp_path / "runs").iterdir()).joinpath("report.md").read_text(encoding="utf-8")
    assert "约5小时" in report and "离线模式" in report


def test_no_fact_check_flag(tmp_path, fake_factory, scenario_path):
    factory = fake_factory()
    code, _ = cli(["plan", "--scenario", str(scenario_path), "--no-fact-check"], factory, tmp_path)
    assert code == 0
    from travel_assistant.prompts import FACT_CHECK_SYSTEM

    assert FACT_CHECK_SYSTEM not in factory.created[0].agent_calls


def test_check_fails_when_report_too_short(tmp_path, fake_factory, scenario_path):
    cli(["plan", "--scenario", str(scenario_path)], fake_factory(report_days=7), tmp_path)
    code, out = cli(["check"], tmp_path=tmp_path)
    assert code == 1 and "每日行程缺少 23 天" in out


def test_args_only_and_overrides(tmp_path, fake_factory):
    factory = fake_factory(report_days=7)
    code, out = cli(
        ["plan", "--destination", "日本 东京+京都", "--days", "7", "--interests", "美食,动漫", "--budget", "3 万"],
        factory, tmp_path,
    )
    assert code == 0, out
    planner_prompt = factory.created[0].planner_prompts[0]
    assert "日本 东京+京都" in planner_prompt and "美食、动漫" in planner_prompt and "3 万" in planner_prompt
    code, out = cli(["check"], tmp_path=tmp_path)
    assert code == 0, out


def test_dry_run_then_resume(tmp_path, fake_factory, scenario_path):
    code, out = cli(["plan", "--scenario", str(scenario_path), "--dry-run"], fake_factory(), tmp_path)
    assert code == 0 and "仅生成任务板" in out
    code, out = cli(["list"], tmp_path=tmp_path)
    assert "planned" in out and "0/5" in out

    code, out = cli(["resume"], fake_factory(), tmp_path)
    assert code == 0 and "最终方案已生成" in out
    code, out = cli(["resume"], fake_factory(), tmp_path)
    assert code == 0 and "无需续跑" in out


def test_failed_run_exit_code_and_resume(tmp_path, fake_factory, scenario_path):
    code, out = cli(["plan", "--scenario", str(scenario_path)], fake_factory(fail_tasks={"food_guide": 9}), tmp_path)
    assert code == 1 and "运行未完成" in out
    code, out = cli(["check"], tmp_path=tmp_path)
    assert code == 1 and "food_guide" in out
    code, out = cli(["resume"], fake_factory(), tmp_path)
    assert code == 0


def test_init_and_errors(tmp_path):
    target = tmp_path / "trip.json"
    code, _ = cli(["init", str(target)], tmp_path=tmp_path)
    assert code == 0 and json.loads(target.read_text(encoding="utf-8"))["days"] == 7
    code, out = cli(["init", str(target)], tmp_path=tmp_path)
    assert code == 1 and "已存在" in out

    code, out = cli(["plan", "--days", "3"], tmp_path=tmp_path)
    assert code == 2 and "--destination" in out
    code, out = cli(["show"], tmp_path=tmp_path)
    assert code == 2 and "还没有任何运行记录" in out
