from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

DEFAULT_AGENTS: dict[str, str] = {
    "route_planner": "整体路线：城市顺序、每地停留天数、季节与节奏安排",
    "flight_expert": "国际/国内机票、城际交通（航班、火车、租车、自驾）",
    "hotel_expert": "住宿：各城市推荐区域、酒店档次与价位、订房策略",
    "food_expert": "美食：各城市代表餐厅、特色菜、预算与预约建议",
    "attraction_expert": "玩乐：标志性景点、国家公园、主题乐园、演出与门票",
    "education_expert": "教育：名校参观、博物馆、科技馆、研学与亲子学习项目",
    "budget_reviewer": "预算汇总、签证保险、安全与风险、预订时间线",
}

DEFAULT_ASPECTS = ["机票", "酒店", "美食", "玩乐", "教育", "交通", "预算"]


@dataclass
class TripRequest:
    """一次旅行规划请求。可从 JSON 场景文件加载，也可由命令行参数覆盖。"""

    title: str
    destination: str
    days: int
    departure_city: str = "上海"
    travelers: str = "2 位成人"
    budget: str = "未指定"
    travel_month: str = "未指定"
    interests: list[str] = field(default_factory=list)
    aspects: list[str] = field(default_factory=lambda: list(DEFAULT_ASPECTS))
    must_see: list[str] = field(default_factory=list)
    notes: str = ""
    language: str = "中文"
    agents: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_AGENTS))
    min_tasks: int = 4
    max_tasks: int = 12

    def __post_init__(self) -> None:
        if self.days < 1:
            raise ValueError("days 必须 >= 1")
        if not self.destination.strip():
            raise ValueError("destination 不能为空")
        if not self.agents:
            raise ValueError("agents 至少需要一个子 Agent")
        if not (1 <= self.min_tasks <= self.max_tasks):
            raise ValueError("需要满足 1 <= min_tasks <= max_tasks")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def brief(self) -> str:
        """给主 Agent 看的完整需求描述。子 Agent 不会直接拿到它。"""

        lines = [
            f"行程标题: {self.title}",
            f"目的地: {self.destination}",
            f"天数: {self.days} 天",
            f"出发城市: {self.departure_city}",
            f"出行人: {self.travelers}",
            f"预算: {self.budget}",
            f"出行月份: {self.travel_month}",
            f"需要覆盖的方面: {'、'.join(self.aspects)}",
        ]
        if self.interests:
            lines.append(f"兴趣偏好: {'、'.join(self.interests)}")
        if self.must_see:
            lines.append(f"必去: {'、'.join(self.must_see)}")
        if self.notes:
            lines.append(f"补充说明: {self.notes}")
        lines.append(f"输出语言: {self.language}")
        return "\n".join(lines)


_LIST_FIELDS = ("interests", "aspects", "must_see")


def load_scenario(path: Path) -> TripRequest:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"{path} 顶层必须是 JSON 对象")
    return request_from_dict(raw)


def request_from_dict(raw: dict[str, Any]) -> TripRequest:
    known = set(TripRequest.__dataclass_fields__)
    unknown = sorted(set(raw) - known)
    if unknown:
        raise ValueError(f"场景文件包含未知字段: {', '.join(unknown)}")
    data = dict(raw)
    for key in _LIST_FIELDS:
        if isinstance(data.get(key), str):
            data[key] = split_list(data[key])
    if "days" in data:
        data["days"] = int(data["days"])
    return TripRequest(**data)


def split_list(value: str) -> list[str]:
    for sep in ("，", "、", ";", "；"):
        value = value.replace(sep, ",")
    return [item.strip() for item in value.split(",") if item.strip()]


def apply_overrides(request: TripRequest, overrides: dict[str, Any]) -> TripRequest:
    data = request.to_dict()
    for key, value in overrides.items():
        if value is None:
            continue
        data[key] = split_list(value) if key in _LIST_FIELDS and isinstance(value, str) else value
    return request_from_dict(data)


def scenario_template() -> dict[str, Any]:
    template = TripRequest(
        title="我的旅行",
        destination="日本 东京+京都+大阪",
        days=7,
        interests=["美食", "历史文化"],
        must_see=["浅草寺", "伏见稻荷大社"],
        notes="第一次去，希望节奏轻松。",
    ).to_dict()
    return template
