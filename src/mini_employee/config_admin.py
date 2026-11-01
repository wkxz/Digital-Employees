"""员工配置的权限、留痕与版本管理。

- 只有管理员（口令见 .env 的 MINI_ADMIN_PASSWORD）能通过界面保存配置；
- 每次保存在 config_changes 表中记录谁、何时、改了哪个文件，以及改前改后的全文，可查看差异、一键回退；
  绕过界面直接改文件的，下次发起任务时也会被发现并记一笔；
- 任务发起时把员工配置复制一份到任务目录，整个任务按这份执行：改配置只影响之后新建的任务。
"""
from __future__ import annotations

import hashlib
import hmac
import os
import shutil
from pathlib import Path
from typing import Any

import yaml

from . import config as C
from .store import Store
from .validate import validate

EXTERNAL = "（直接修改文件，未经配置页）"
BASELINE = "（首次记录的版本）"


def admin_configured() -> bool:
    return bool(os.environ.get("MINI_ADMIN_PASSWORD"))


def check_admin(password: str | None) -> bool:
    expected = os.environ.get("MINI_ADMIN_PASSWORD", "")
    return bool(expected) and hmac.compare_digest((password or "").encode(), expected.encode())


def editable_files(employee_id: str) -> list[str]:
    """界面可编辑的文件：employee.yaml 和它引用的 SKILL.md（只允许这两个，防止借文件名写到别处）。"""
    _, raw, _ = C.read_employee_files(employee_id)
    return ["employee.yaml", raw.get("skill", "SKILL.md")]


def _sha(text: str | None) -> str | None:
    return hashlib.sha256(text.encode("utf-8")).hexdigest() if text is not None else None


def config_sha(d: Path) -> str:
    """员工目录内全部配置文件的合并哈希，记录在任务里，说明这份报告按哪个版本的配置生成。"""
    h = hashlib.sha256()
    for p in sorted(x for x in d.rglob("*") if x.is_file()):
        h.update(p.relative_to(d).as_posix().encode() + b"\0" + p.read_bytes() + b"\0")
    return h.hexdigest()


def validate_texts(employee_id: str, name: str, text: str) -> tuple[list[str], list[str]]:
    """校验“改后的整套配置”（一个文件用新内容，另一个用磁盘上的现有内容）。"""
    d, raw, skill = C.read_employee_files(employee_id)
    try:
        if name == "employee.yaml":
            raw = yaml.safe_load(text) or {}
        else:
            skill = text
        return validate(raw, C.parse_skill_md(skill)[1])
    except Exception as e:  # noqa: BLE001  YAML 语法错误
        return [f"{type(e).__name__}: {e}"], []


def record_external_changes(store: Store, employee_id: str) -> None:
    """磁盘上的文件与最后一次记录的内容不同（有人直接改了文件）时补记一笔；从未记录过的文件记为首个版本。"""
    d = C.EMPLOYEES_DIR / employee_id
    for name in editable_files(employee_id):
        p = d / name
        if not p.exists():
            continue
        text = p.read_text(encoding="utf-8")
        last = store.last_config_change(employee_id, name)
        if last is None:
            store.config_change(employee_id, name, BASELINE, None, text)
        elif last["content_after"] != text:
            store.config_change(employee_id, name, EXTERNAL, last["content_after"], text)


def save(store: Store, employee_id: str, name: str, text: str, user: str, password: str | None,
         note: str = "") -> tuple[list[str], list[str], int | None]:
    """校验通过且口令正确才写盘并留痕。返回 (错误, 提醒, 变更序号)。"""
    if not check_admin(password):
        return ["只有管理员可以修改员工配置（管理员口令不正确或未设置 MINI_ADMIN_PASSWORD）"], [], None
    if name not in editable_files(employee_id):
        return [f"不允许修改 {name}"], [], None
    errors, warnings = validate_texts(employee_id, name, text)
    if errors:
        return errors, warnings, None
    record_external_changes(store, employee_id)  # 先把别人直接改文件的情况记下，本次变更的“改前”才准确
    p = C.EMPLOYEES_DIR / employee_id / name
    before = p.read_text(encoding="utf-8")
    if before == text:
        return [], warnings + ["内容没有变化，未保存"], None
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8", newline="\n")
    tmp.replace(p)
    return [], warnings, store.config_change(employee_id, name, user or "管理员", before, text, note)


def rollback(store: Store, employee_id: str, seq: int, user: str, password: str | None
             ) -> tuple[list[str], list[str], int | None]:
    """把某次变更涉及的文件恢复到该次变更之前的内容；回退本身也是一次变更，同样校验、留痕。"""
    ch = store.get_config_change(seq)
    if not ch or ch["employee"] != employee_id:
        return [f"没有序号为 {seq} 的配置变更"], [], None
    if ch["content_before"] is None:
        return ["这是首次记录的版本，没有更早的内容可以回退"], [], None
    return save(store, employee_id, ch["file"], ch["content_before"], user, password, note=f"回退变更 #{seq}")


def snapshot(store: Store, employee_id: str, task_dir: Path) -> str:
    """把员工配置复制到任务目录（覆盖旧快照），返回配置哈希。"""
    record_external_changes(store, employee_id)
    src = C.EMPLOYEES_DIR / employee_id
    dst = task_dir / "config" / employee_id
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst, ignore=shutil.ignore_patterns("*.tmp", "__pycache__"))
    return config_sha(dst)


def history(store: Store, employee_id: str, limit: int = 50) -> list[dict[str, Any]]:
    return store.config_changes(employee_id, limit)
