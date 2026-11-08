"""子 Agent 可调用的真实数据工具：网页搜索、地点查询、驾车路线。

都使用免费、无需 API Key 的公开服务：
- 网页搜索：ddgs（聚合 Google / Yahoo 等搜索引擎结果）
- 地点查询：OpenStreetMap Nominatim
- 驾车路线：OSRM 公共路由服务

每次调用都会记录来源，供最终报告引用和事后核查。
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Awaitable, Callable, Protocol

USER_AGENT = "multi-agent-travel-planner/0.2 (+https://github.com/zhenyuduan98/multi-agent-travel-planner)"
SEARCH_BACKENDS = ("google", "yahoo")
_AD_MARKERS = ("bing.com/aclick", "googleadservices.com", "doubleclick.net", "/aclk?", "r.search.yahoo.com/cbclk")


def _is_ad(url: str) -> bool:
    return any(marker in url for marker in _AD_MARKERS)


@dataclass(frozen=True)
class Source:
    title: str
    url: str
    snippet: str = ""

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


@dataclass
class ToolResult:
    """content 交给模型阅读；sources 记录到任务卡上。"""

    content: str
    sources: list[Source] = field(default_factory=list)
    ok: bool = True


class ToolBackend(Protocol):
    async def search(self, query: str, max_results: int) -> list[Source]: ...

    async def geocode(self, query: str) -> dict[str, Any] | None: ...

    async def route(self, start: tuple[float, float], end: tuple[float, float]) -> dict[str, float] | None: ...


class PublicBackend:
    """真实网络实现。Nominatim 要求每秒不超过 1 次请求，这里串行限速。"""

    def __init__(self, timeout: float = 20.0) -> None:
        import httpx

        self._http = httpx.AsyncClient(timeout=timeout, headers={"User-Agent": USER_AGENT})
        self._nominatim_lock = asyncio.Lock()
        self._last_nominatim = 0.0
        self._search_lock = asyncio.Semaphore(2)

    async def search(self, query: str, max_results: int) -> list[Source]:
        from ddgs import DDGS

        def run() -> list[dict[str, Any]]:
            for backend in SEARCH_BACKENDS:
                try:
                    results = DDGS().text(query, max_results=max_results, backend=backend)
                except Exception:
                    continue
                if results:
                    return results
            return []

        async with self._search_lock:
            raw = await asyncio.to_thread(run)
        return [
            Source(title=str(r.get("title", ""))[:200], url=str(r.get("href", "")), snippet=str(r.get("body", ""))[:400])
            for r in raw
            if r.get("href") and not _is_ad(str(r["href"]))
        ]

    async def geocode(self, query: str) -> dict[str, Any] | None:
        async with self._nominatim_lock:
            wait = 1.1 - (time.monotonic() - self._last_nominatim)
            if wait > 0:
                await asyncio.sleep(wait)
            response = await self._http.get(
                "https://nominatim.openstreetmap.org/search",
                params={"q": query, "format": "jsonv2", "limit": 1, "addressdetails": 0},
            )
            self._last_nominatim = time.monotonic()
        response.raise_for_status()
        items = response.json()
        if not items:
            return None
        item = items[0]
        return {
            "name": item.get("display_name", ""),
            "lat": float(item["lat"]),
            "lon": float(item["lon"]),
            "osm_url": f"https://www.openstreetmap.org/{item.get('osm_type', 'node')}/{item.get('osm_id', '')}",
        }

    async def route(self, start: tuple[float, float], end: tuple[float, float]) -> dict[str, float] | None:
        (lat1, lon1), (lat2, lon2) = start, end
        response = await self._http.get(
            f"https://router.project-osrm.org/route/v1/driving/{lon1},{lat1};{lon2},{lat2}",
            params={"overview": "false"},
        )
        response.raise_for_status()
        routes = response.json().get("routes") or []
        if not routes:
            return None
        return {"km": routes[0]["distance"] / 1000, "hours": routes[0]["duration"] / 3600}

    async def aclose(self) -> None:
        await self._http.aclose()


TOOL_SPECS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": (
                "搜索互联网，用于核实餐厅/酒店/景点是否存在、所在城市与地址、门票与营业时间、预约规则等。"
                "用英文关键词搜索美国等海外地点效果更好。返回标题、链接和摘要。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "搜索关键词，例如 'The Ahwahnee Yosemite hotel official site'"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "lookup_place",
            "description": "在 OpenStreetMap 中查询一个地点（餐厅、酒店、景点、城市），返回规范名称、完整地址和坐标。查不到说明名称可能有误。",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "地点名称加城市，例如 'Katz's Delicatessen, New York'"},
                },
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "drive_route",
            "description": "计算两地之间的驾车距离和时间（不含堵车和休息）。用于核实城际自驾、国家公园接驳车程。",
            "parameters": {
                "type": "object",
                "properties": {
                    "origin": {"type": "string", "description": "出发地，例如 'San Francisco, CA'"},
                    "destination": {"type": "string", "description": "目的地，例如 'Yosemite Valley, CA'"},
                },
                "required": ["origin", "destination"],
            },
        },
    },
]


class ToolBox:
    """把模型的工具调用分发到后端，带缓存（同一次运行内相同查询只请求一次）。"""

    def __init__(self, backend: ToolBackend, max_results: int = 5) -> None:
        self.backend = backend
        self.max_results = max_results
        self._cache: dict[tuple[str, str], ToolResult] = {}
        self._geo_cache: dict[str, dict[str, Any] | None] = {}
        self.specs = TOOL_SPECS

    async def call(self, name: str, arguments: str | dict[str, Any]) -> ToolResult:
        try:
            args = json.loads(arguments) if isinstance(arguments, str) else dict(arguments)
        except json.JSONDecodeError:
            return ToolResult(f"参数不是合法 JSON: {arguments!r}", ok=False)

        key = (name, json.dumps(args, ensure_ascii=False, sort_keys=True))
        if key in self._cache:
            return self._cache[key]
        handler: Callable[[dict[str, Any]], Awaitable[ToolResult]] | None = {
            "web_search": self._web_search,
            "lookup_place": self._lookup_place,
            "drive_route": self._drive_route,
        }.get(name)
        if handler is None:
            return ToolResult(f"未知工具: {name}", ok=False)
        try:
            result = await handler(args)
        except Exception as exc:
            return ToolResult(f"工具调用失败（{type(exc).__name__}: {str(exc)[:200]}），请换个关键词或标注为待核实。", ok=False)
        self._cache[key] = result
        return result

    async def _geo(self, query: str) -> dict[str, Any] | None:
        if query not in self._geo_cache:
            self._geo_cache[query] = await self.backend.geocode(query)
        return self._geo_cache[query]

    async def _web_search(self, args: dict[str, Any]) -> ToolResult:
        query = str(args.get("query", "")).strip()
        if not query:
            return ToolResult("query 不能为空", ok=False)
        sources = await self.backend.search(query, self.max_results)
        if not sources:
            return ToolResult(f"没有搜到与 {query!r} 相关的结果。", ok=False)
        lines = [f"[{i}] {s.title}\n    {s.url}\n    {s.snippet}" for i, s in enumerate(sources, start=1)]
        return ToolResult("\n".join(lines), sources)

    async def _lookup_place(self, args: dict[str, Any]) -> ToolResult:
        name = str(args.get("name", "")).strip()
        if not name:
            return ToolResult("name 不能为空", ok=False)
        place = await self._geo(name)
        if place is None:
            return ToolResult(f"OpenStreetMap 中没有找到 {name!r}，该名称可能不存在或拼写有误。", ok=False)
        return ToolResult(
            f"{place['name']}（坐标 {place['lat']:.5f}, {place['lon']:.5f}）",
            [Source(title=f"OpenStreetMap: {name}", url=place["osm_url"], snippet=place["name"])],
        )

    async def _drive_route(self, args: dict[str, Any]) -> ToolResult:
        origin = str(args.get("origin", "")).strip()
        destination = str(args.get("destination", "")).strip()
        if not origin or not destination:
            return ToolResult("origin 和 destination 都不能为空", ok=False)
        start, end = await self._geo(origin), await self._geo(destination)
        missing = [n for n, p in ((origin, start), (destination, end)) if p is None]
        if missing:
            return ToolResult(f"找不到地点: {', '.join(missing)}", ok=False)
        assert start and end
        route = await self.backend.route((start["lat"], start["lon"]), (end["lat"], end["lon"]))
        if route is None:
            return ToolResult(f"OSRM 无法规划 {origin} → {destination} 的驾车路线。", ok=False)
        url = (
            "https://www.openstreetmap.org/directions?engine=fossgis_osrm_car"
            f"&route={start['lat']:.5f},{start['lon']:.5f};{end['lat']:.5f},{end['lon']:.5f}"
        )
        text = f"{origin} → {destination}: 约 {route['km']:.0f} 公里，纯驾驶约 {route['hours']:.1f} 小时（不含堵车与休息）"
        return ToolResult(text, [Source(title=f"OSRM 驾车路线: {origin} → {destination}", url=url, snippet=text)])

    async def aclose(self) -> None:
        close = getattr(self.backend, "aclose", None)
        if close is not None:
            await close()
