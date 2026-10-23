"""项目路径与员工配置加载。

员工 = 一个目录：employee.yaml（结构：表单、步骤、工具、质检、上限）
+ SKILL.md（做法：岗位职责、步骤要求、注意事项）。业务人员只改这两个文件。
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(os.environ.get("MINI_HOME", Path(__file__).resolve().parents[2]))
EMPLOYEES_DIR = ROOT / "employees"
ASSETS_DIR = ROOT / "assets"


def runs_dir() -> Path:
    d = Path(os.environ.get("MINI_RUNS", ROOT / "runs"))
    d.mkdir(parents=True, exist_ok=True)
    return d


@dataclass
class Employee:
    id: str
    name: str
    dir: Path
    raw: dict[str, Any]
    skill_meta: dict[str, Any] = field(default_factory=dict)
    skill_sections: dict[str, str] = field(default_factory=dict)

    def __getattr__(self, key: str) -> Any:
        # employee.form / employee.limits ... 直接取 yaml 顶层键
        try:
            return self.__dict__["raw"][key]
        except KeyError as e:
            raise AttributeError(key) from e

    def limit(self, key: str) -> Any:
        return self.raw["limits"][key]

    def step(self, step_id: str) -> dict[str, Any]:
        for s in self.raw["steps"]:
            if s["id"] == step_id:
                return s
        raise KeyError(step_id)

    def skill(self, *sections: str) -> str:
        parts = []
        for name in sections:
            if name in self.skill_sections:
                parts.append(f"## {name}\n{self.skill_sections[name]}")
        return "\n\n".join(parts)


def parse_skill_md(text: str) -> tuple[dict[str, Any], dict[str, str]]:
    """SKILL.md：可选 YAML frontmatter + 以“## 标题”划分的小节。"""
    meta: dict[str, Any] = {}
    m = re.match(r"^---\n(.*?)\n---\n", text, re.S)
    if m:
        meta = yaml.safe_load(m.group(1)) or {}
        text = text[m.end():]
    sections: dict[str, str] = {}
    current = None
    buf: list[str] = []
    for line in text.splitlines():
        h = re.match(r"^##\s+(.+?)\s*$", line)
        if h:
            if current:
                sections[current] = "\n".join(buf).strip()
            current, buf = h.group(1), []
        elif current:
            buf.append(line)
    if current:
        sections[current] = "\n".join(buf).strip()
    return meta, sections


def load_employee(employee_id: str, base: Path | None = None) -> Employee:
    d = (base or EMPLOYEES_DIR) / employee_id
    raw = yaml.safe_load((d / "employee.yaml").read_text(encoding="utf-8"))
    meta, sections = parse_skill_md((d / raw.get("skill", "SKILL.md")).read_text(encoding="utf-8"))
    return Employee(id=raw["id"], name=raw["name"], dir=d, raw=raw, skill_meta=meta, skill_sections=sections)


def list_employees() -> list[str]:
    return sorted(p.name for p in EMPLOYEES_DIR.iterdir() if (p / "employee.yaml").exists())
