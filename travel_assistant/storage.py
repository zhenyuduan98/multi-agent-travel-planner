from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

from travel_assistant.orchestrator import RunState, dumps, state_from_dict


def default_runs_dir() -> Path:
    return Path(os.getenv("TRAVEL_RUNS_DIR", ".travel_runs")).resolve()


def slugify(text: str) -> str:
    slug = re.sub(r"[^0-9a-zA-Z\u4e00-\u9fff]+", "-", text).strip("-").lower()
    return slug[:40] or "trip"


class RunStore:
    """每次运行一个目录: state.json（完整状态，可续跑）+ report.md（最终方案）。"""

    def __init__(self, root: Path | None = None) -> None:
        self.root = root or default_runs_dir()

    def new_run_dir(self, title: str) -> Path:
        run_dir = self.root / f"{time.strftime('%Y%m%d-%H%M%S')}-{slugify(title)}"
        suffix = 1
        while run_dir.exists():
            suffix += 1
            run_dir = run_dir.with_name(f"{run_dir.name}-{suffix}")
        run_dir.mkdir(parents=True)
        return run_dir

    def save(self, run_dir: Path, state: RunState) -> None:
        tmp = run_dir / "state.json.tmp"
        tmp.write_text(dumps(state), encoding="utf-8")
        tmp.replace(run_dir / "state.json")
        if state.report:
            (run_dir / "report.md").write_text(state.report + "\n", encoding="utf-8")

    def load(self, run_dir: Path) -> RunState:
        path = run_dir / "state.json"
        if not path.exists():
            raise FileNotFoundError(f"找不到运行记录: {path}")
        return state_from_dict(json.loads(path.read_text(encoding="utf-8")))

    def list_runs(self) -> list[Path]:
        if not self.root.exists():
            return []
        return sorted((p for p in self.root.iterdir() if (p / "state.json").exists()), reverse=True)

    def resolve(self, ref: str | None) -> Path:
        """ref 可以是 'latest'、目录名、目录名前缀或路径。"""

        runs = self.list_runs()
        if not ref or ref == "latest":
            if not runs:
                raise FileNotFoundError(f"{self.root} 下还没有任何运行记录")
            return runs[0]
        direct = Path(ref)
        if (direct / "state.json").exists():
            return direct.resolve()
        matches = [p for p in runs if p.name == ref] or [p for p in runs if p.name.startswith(ref)]
        if len(matches) == 1:
            return matches[0]
        if not matches:
            raise FileNotFoundError(f"找不到运行记录: {ref}")
        raise FileNotFoundError(f"'{ref}' 匹配到多条记录: {', '.join(p.name for p in matches[:5])}")
