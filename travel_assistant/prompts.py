from __future__ import annotations

import json
from typing import Any

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

WORKER_SYSTEM_WITH_TOOLS = (
    "你是旅行规划团队里的一名专业子 Agent。你只能看到主 Agent 发给你的私有任务包，"
    "看不到用户的完整对话和其他任务。\n"
    "你可以调用工具获取真实数据：web_search（网页搜索）、lookup_place（查地点是否存在及地址）、"
    "drive_route（驾车距离与时间）。规则：\n"
    "1. 推荐具体的餐厅、酒店、景点前，用 lookup_place 或 web_search 确认它真实存在、确实在你说的城市。查不到的不要推荐。\n"
    "2. 写到任何自驾或城际车程时，用 drive_route 获取真实时长，不要凭印象估算。\n"
    "3. 同一轮可以并行调用多个工具；工具调用总轮数有限，优先核实最关键、最容易出错的信息。\n"
    "4. 经工具核实的信息在句末标注来源编号，例如“（来源: OSRM）”或“[S1]”；未核实的标注“需核实”。\n"
    "5. 价格、营业时间会变化，即使搜到也写成“参考”，并建议出行前到官网确认。"
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


FACT_CHECK_SYSTEM = (
    "你是旅行方案的事实核查员。你会拿到一份旅行方案，需要用工具核查其中最容易出错的硬事实，"
    "并给出可以直接替换的修正。只修正有工具证据支持的错误，不确定的不要改。只输出 JSON。"
)


def fact_check_prompt(report: str, max_claims: int, language: str) -> str:
    schema = {
        "checked": [
            {
                "claim": "方案中的原句或关键片段",
                "verdict": "correct | wrong | unverified",
                "evidence": "工具返回的依据，一句话",
            }
        ],
        "corrections": [
            {
                "find": "从方案中逐字复制的一小段原文（10-40 字，必须位于同一行内），只出现一次",
                "replace": "修正后的同一段文字",
                "reason": "修正理由，引用工具结果",
            }
        ],
    }
    return (
        f"待核查的旅行方案:\n<<<\n{report}\n>>>\n\n"
        f"请挑选最多 {max_claims} 条最可能出错、且对行程影响最大的硬事实进行核查，优先：\n"
        "1. 餐厅/酒店/景点是否真实存在、是否在方案所写的城市（用 lookup_place）；\n"
        "2. 城际自驾、国家公园接驳的车程（用 drive_route，与方案里的小时数对比，相差超过 30% 视为错误）；\n"
        "3. 明显的名称拼写错误。\n"
        "调用工具完成核查后，只输出如下 JSON（不要输出其他内容）:\n"
        f"{json.dumps(schema, ensure_ascii=False, indent=2)}\n"
        "corrections 的 find 规则（不满足的修正会被丢弃）:\n"
        "- 必须是从方案里**逐字复制**的连续文字，包括标点、空格、方括号里的来源编号，不能改写、不能用省略号、不能跨行；\n"
        "- 只截取包含错误的那一小段，长度 10-40 字，例如“自驾（约4.5h，I-40+Warm Springs Rd”；\n"
        "- 在方案中只能出现一次；同一错误出现在多处时，为每处分别给一条、各自带上能区分的前后文字。\n"
        f"evidence、reason、replace 使用{language}。没有发现错误时 corrections 为空列表。"
    )


def apply_corrections(report: str, corrections: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
    """逐条做字符串替换，只接受在报告中恰好出现一次的 find。

    同一个片段出现多次时（例如两段不同路线都写着“4h45m”），无法确定修正的是哪一处，
    整体替换会把正确的地方改错，因此跳过，留给人工确认。
    返回 (新报告, 实际生效的修正)；被跳过的修正带 skipped 原因记录在 item 上。
    """

    applied: list[dict[str, Any]] = []
    for item in corrections:
        find = str(item.get("find", "")).strip()
        replace = str(item.get("replace", "")).strip()
        if not find or not replace or find == replace:
            item["skipped"] = "find/replace 为空或相同"
            continue
        count = report.count(find)
        if count == 0:
            item["skipped"] = "原文未出现在方案中"
            continue
        if count > 1:
            item["skipped"] = f"原文在方案中出现 {count} 次，无法确定修正哪一处"
            continue
        report = report.replace(find, replace)
        applied.append({**item, "find": find, "replace": replace})
    return report, applied


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
    "保持事实谨慎：价格与营业时间都是参考，需要在文中标注“需核实”。"
)


def synth_prompt(request: TripRequest, board: Board, per_task_limit: int = 1800, grounded: bool = False) -> str:
    sections = []
    for card in board.cards:
        body = card.result if len(card.result) <= per_task_limit else card.result[:per_task_limit] + "…"
        sections.append(f"### [{card.task_id}] {card.title}（{card.agent}）\n{body}")
    rules = (
        "规则:\n"
        "1. 只使用子 Agent 成果中出现过的餐厅、酒店、景点名称，不要自行添加新名称。\n"
        "2. 车程、距离优先采用子 Agent 标注了来源（如 OSRM）的数字，不要改写成其他数字。\n"
        "3. 路线总览、每日行程、交通章节中的城市顺序和自驾方向必须一致。\n"
        "4. 保留子 Agent 的来源标注；不要编造“已确认”“已预订”等字样。\n"
        if grounded
        else "不要编造“已确认”“已预订”等字样。"
    )
    return (
        f"用户需求:\n{request.brief()}\n\n"
        f"各子 Agent 成果:\n\n" + "\n\n".join(sections) + "\n\n"
        f"请用{request.language}输出一份完整的 Markdown 旅行方案，包含以下章节（按顺序）:\n"
        "# 标题\n"
        "## 行程总览（一段话 + 城市路线与天数表）\n"
        f"## 每日行程（表格，第 1 天到第 {request.days} 天每天单独一行，共 {request.days} 行，"
        "不得合并多天、不得用“返程缓冲期”等省略；列：天数、城市、主要安排、餐饮、住宿区域）\n"
        "## 机票与城际交通\n## 住宿建议\n## 美食清单\n## 玩乐与门票\n## 教育与研学\n"
        "## 预算估算（表格，按类别给区间）\n## 预订时间线与清单\n## 风险与待核实事项\n"
        + rules
    )


def missing_days(report: str, days: int) -> list[int]:
    """每日行程里没有单独出现的天数。识别“第 N 天”“第N天”“Day N”“DN”。"""

    import re

    found: set[int] = set()
    for pattern in (r"第\s*(\d{1,3})\s*天", r"\bDay\s*(\d{1,3})\b", r"\*\*D(\d{1,3})\*\*"):
        found.update(int(n) for n in re.findall(pattern, report))
    return [d for d in range(1, days + 1) if d not in found]


def repair_days_prompt(report: str, days: int, missing: list[int]) -> str:
    return (
        f"下面这份旅行方案的「每日行程」没有逐日覆盖全部 {days} 天，缺少: "
        + "、".join(f"第 {d} 天" for d in missing)
        + "。\n请重写「## 每日行程」整个章节：表格中第 1 天到第 "
        f"{days} 天每天单独一行（共 {days} 行），保持与方案其他章节的城市顺序、交通和住宿一致，"
        "不要新增方案里没有出现过的餐厅、酒店名称。只输出重写后的「## 每日行程」章节（从标题开始），不要输出其他内容。\n\n"
        f"方案:\n<<<\n{report}\n>>>"
    )


def replace_section(report: str, heading: str, new_section: str) -> str:
    """用 new_section 替换 report 中以 heading 开头的二级章节；找不到时原样返回。"""

    start = report.find(heading)
    if start < 0 or heading not in new_section:
        return report
    new_section = new_section[new_section.find(heading) :].strip()
    end = report.find("\n## ", start + len(heading))
    end = len(report) if end < 0 else end
    return report[:start] + new_section + "\n\n" + report[end:].lstrip("\n")


def _claim_was_fixed(claim: str, applied: list[dict[str, Any]]) -> bool:
    """claim 与已应用修正的 find 常常互为子串、或只差来源编号等尾部，比较时去掉空白和标点再看包含关系。"""

    import re

    def norm(text: str) -> str:
        return re.sub(r"[\s\[\]()（）,，。;；:：、|]+", "", text)

    target = norm(claim)
    if not target:
        return False
    for item in applied:
        find = norm(str(item.get("find", "")))
        if find and (find in target or target in find):
            return True
    return False


def sources_appendix(board: Board, fact_check: dict[str, Any] | None) -> str:
    """附在报告末尾：每个子 Agent 实际查询过的来源 + 事实核查结果。"""

    lines = ["", "---", "", "## 数据来源与事实核查", ""]
    any_source = False
    for card in board.cards:
        if not card.sources:
            continue
        any_source = True
        lines.append(f"**{card.task_id}**（{card.agent}，调用工具 {len(card.tool_calls)} 次）")
        for source in card.sources[:12]:
            title = source.get("title", "").replace("[", "(").replace("]", ")")
            lines.append(f"- [{title}]({source.get('url', '')})")
        lines.append("")
    if not any_source:
        lines.append("_本次运行未使用联网工具（离线模式）。_")
        lines.append("")

    if fact_check:
        applied = fact_check.get("applied", [])
        checked = fact_check.get("checked", [])
        lines.append(f"**事实核查**：核查 {len(checked)} 条，自动修正 {len(applied)} 处。")
        lines.append("")
        if applied:
            lines.append("| 原文 | 修正为 | 依据 |")
            lines.append("|---|---|---|")
            for item in applied:
                cells = [str(item.get(k, "")).replace("|", "/").replace("\n", " ") for k in ("find", "replace", "reason")]
                lines.append(f"| {cells[0]} | {cells[1]} | {cells[2]} |")
            lines.append("")
        unresolved = [c for c in checked if c.get("verdict") in ("wrong", "unverified")]
        unapplied = [c for c in unresolved if not _claim_was_fixed(str(c.get("claim", "")), applied)]
        skipped = [p for p in fact_check.get("proposed", []) if p.get("skipped")]
        if unapplied or skipped:
            lines.append("仍需人工确认:")
            for item in unapplied:
                lines.append(f"- {item.get('claim', '')}（{item.get('verdict', '')}：{item.get('evidence', '')}）")
            for item in skipped:
                lines.append(
                    f"- 建议把“{item.get('find', '')}”改为“{item.get('replace', '')}”"
                    f"（{item.get('reason', '')}；未自动应用：{item['skipped']}）"
                )
            lines.append("")
    return "\n".join(lines)
