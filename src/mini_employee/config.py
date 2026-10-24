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


def load_dotenv(path: Path) -> None:
    """读取项目根目录的 .env（KEY=VALUE），不覆盖已存在的环境变量。.env 已加入 .gitignore，密钥不会进仓库。"""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        k, v = k.strip(), v.strip().strip('"').strip("'")
        if k and v and k not in os.environ:
            os.environ[k] = v


load_dotenv(ROOT / ".env")
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


def read_employee_files(employee_id: str, base: Path | None = None) -> tuple[Path, dict[str, Any], str]:
    """读取原始配置（不校验）：(目录, employee.yaml 内容, SKILL.md 文本)。"""
    d = (base or EMPLOYEES_DIR) / employee_id
    raw = yaml.safe_load((d / "employee.yaml").read_text(encoding="utf-8")) or {}
    skill = d / raw.get("skill", "SKILL.md")
    return d, raw, skill.read_text(encoding="utf-8") if skill.exists() else ""


def load_employee(employee_id: str, base: Path | None = None) -> Employee:
    """加载并校验员工配置；有错误时抛出 ConfigError，列出全部问题。"""
    from .validate import ConfigError, validate

    d, raw, skill_text = read_employee_files(employee_id, base)
    meta, sections = parse_skill_md(skill_text)
    errors, _ = validate(raw, sections)
    if errors:
        raise ConfigError(employee_id, errors)
    return Employee(id=raw["id"], name=raw["name"], dir=d, raw=raw, skill_meta=meta, skill_sections=sections)


def list_employees() -> list[str]:
    return sorted(p.name for p in EMPLOYEES_DIR.iterdir() if (p / "employee.yaml").exists())
