from __future__ import annotations

import json

from travel_assistant.board import Board, TaskCard
from travel_assistant.scenario import TripRequest

PLANNER_SYSTEM = (
    "你是旅行规划团队的主 Agent（总调度）。你的职责是把用户需求拆成一张可执行的任务板，"
    "分派给专业子 Agent。你只输出 JSON，不输出任何解释。"
)


def planner_prompt(request: TripRequest, previous_error: str = "") -> str:
    agents = "\n".join(f"- {name}: {duty}" for name, duty in request.agents.items())
    schema = {
        "tasks": [
            {
                "task_id": "lower_snake_case 唯一 id，例如 plan_route、book_flights",
                "title": "简短标题",
                "agent": "必须是下方子 Agent 名称之一",
                "goal": "该子 Agent 要完成的局部目标（具体、可交付，≤150 字）",
                "context": "子 Agent 需要知道的需求要点（只写与本任务相关的，≤300 字）",
                "depends_on": ["上游 task_id，没有则为空列表"],
            }
        ]
    }
    text = (
        f"用户需求:\n{request.brief()}\n\n"
        f"可用子 Agent:\n{agents}\n\n"
        "拆解要求:\n"
        f"1. 任务数量 {request.min_tasks}-{request.max_tasks} 张，覆盖用户需要的所有方面。\n"
        "2. 每张卡只交给一个子 Agent，目标要具体，能独立完成。\n"
        "3. 设计合理的 depends_on：通常先定路线，再并行做机票/酒店/美食/玩乐/教育，最后做预算与风险复核。\n"
        "4. 依赖必须无环，只能引用本任务板中存在的 task_id。\n"
        "5. 子 Agent 看不到用户原始需求，context 里必须写清它需要的天数、人数、预算、偏好等要点。\n"
        "6. 子 Agent 不能联网，涉及实时价格/余票/营业时间的内容要在 goal 里注明“给出参考区间并标注需核实”。\n\n"
        f"只输出如下结构的 JSON:\n{json.dumps(schema, ensure_ascii=False, indent=2)}"
    )
    if previous_error:
        text += f"\n\n你上一次的输出没有通过校验，原因: {previous_error}\n请修正后重新输出完整 JSON。"
    return text


WORKER_SYSTEM = (
    "你是旅行规划团队里的一名专业子 Agent。你只能看到主 Agent 发给你的私有任务包，"
    "看不到用户的完整对话和其他任务。你不能联网，不能声称已实时查证；"
    "价格、营业时间、余票等易变信息给出参考区间并标注“需核实”。"
)


def worker_prompt(card: TaskCard, duty: str, upstream: dict[str, str], language: str, max_chars: int) -> str:
    upstream_text = "\n\n".join(f"[{tid}]\n{text}" for tid, text in upstream.items()) or "（无）"
    return (
        f"你的角色: {card.agent} —— {duty}\n\n"
        f"任务: {card.title}\n"
        f"目标: {card.goal}\n\n"
        f"背景: {card.context}\n\n"
        f"上游任务的交接摘要:\n{upstream_text}\n\n"
        f"请用{language}、Markdown 输出，总长度不超过 {max_chars} 字，固定包含三个小节:\n"
        "## 结论\n（具体建议，尽量给出名称、区域、参考价格区间、时长）\n"
        "## 风险与待核实\n（列出需要用户或实时渠道确认的点）\n"
        "## 交接摘要\n（≤200 字，给下游任务直接使用的关键信息）"
    )


def handoff_summary(result: str, limit: int = 600) -> str:
    """下游只拿上游的「交接摘要」小节；没有该小节时退化为截断全文。"""

    marker = "## 交接摘要"
    index = result.find(marker)
    text = result[index + len(marker) :] if index >= 0 else result
    next_heading = text.find("\n## ")
    if index >= 0 and next_heading >= 0:
        text = text[:next_heading]
    text = text.strip()
    return text if len(text) <= limit else text[:limit] + "…"


SYNTH_SYSTEM = (
    "你是旅行规划团队的主 Agent，负责把各子 Agent 的成果整合成一份可直接使用的旅行方案。"
    "保持事实谨慎：子 Agent 没有联网，所有价格与时间都是参考，需要在文中标注“需核实”。"
)


def synth_prompt(request: TripRequest, board: Board, per_task_limit: int = 1800) -> str:
    sections = []
    for card in board.cards:
        body = card.result if len(card.result) <= per_task_limit else card.result[:per_task_limit] + "…"
        sections.append(f"### [{card.task_id}] {card.title}（{card.agent}）\n{body}")
    return (
        f"用户需求:\n{request.brief()}\n\n"
        f"各子 Agent 成果:\n\n" + "\n\n".join(sections) + "\n\n"
        f"请用{request.language}输出一份完整的 Markdown 旅行方案，包含以下章节（按顺序）:\n"
        "# 标题\n"
        "## 行程总览（一段话 + 城市路线与天数表）\n"
        f"## 每日行程（第 1 天到第 {request.days} 天逐日列出：城市、主要安排、餐饮、住宿区域）\n"
        "## 机票与城际交通\n## 住宿建议\n## 美食清单\n## 玩乐与门票\n## 教育与研学\n"
        "## 预算估算（表格，按类别给区间）\n## 预订时间线与清单\n## 风险与待核实事项\n"
        "不要编造“已确认”“已预订”等字样。"
    )
