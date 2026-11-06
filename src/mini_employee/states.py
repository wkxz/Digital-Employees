"""任务状态机：状态、流转表、闸门条件。

所有状态变化只能通过 Store.transition() 按本表进行；表中没有的流转、闸门不满足的流转一律拒绝并留痕。
模型不能触发任何流转事件——触发方只有发起人、审核人和系统（程序）。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Callable

from .config import runs_dir


class S(StrEnum):
    CREATED = "CREATED"
    ADMISSION = "ADMISSION"
    WAITING_INPUT = "WAITING_INPUT"
    RUNNING = "RUNNING"
    QC = "QC"
    AUTO_FIXING = "AUTO_FIXING"
    SUSPENDED = "SUSPENDED"
    PENDING_REVIEW = "PENDING_REVIEW"
    REWORK = "REWORK"
    DELIVERED = "DELIVERED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"


LABELS = {
    S.CREATED: "已创建", S.ADMISSION: "准入校验", S.WAITING_INPUT: "待补材料", S.RUNNING: "执行中",
    S.QC: "自动质检", S.AUTO_FIXING: "自动修正", S.SUSPENDED: "暂停转人工", S.PENDING_REVIEW: "待审核",
    S.REWORK: "返工中", S.DELIVERED: "已交付", S.REJECTED: "已拒绝", S.CANCELLED: "已取消",
}
TERMINAL = {S.DELIVERED, S.REJECTED, S.CANCELLED}
WAITING = {S.WAITING_INPUT, S.SUSPENDED, S.PENDING_REVIEW}  # 等人处理
AUTO = {S.CREATED, S.ADMISSION, S.RUNNING, S.QC, S.AUTO_FIXING, S.REWORK}  # 程序自动推进，可断点恢复

INITIATOR, REVIEWER, SYSTEM = "initiator", "reviewer", "system"
ADMIN = "admin"  # 管理员：维护员工配置、故障注入；不参与任务流转

Guard = Callable[[dict[str, Any], dict[str, Any]], str | None]  # (task, ctx) -> 拒绝原因 / None


def _qc_record_problem(task: dict) -> str | None:
    """读质检记录本身判定，不信任调用方传入的标志：最近一次质检须全部通过，且检的就是当前这份产物。"""
    art = task.get("artifact") or {}
    path = runs_dir() / task["id"] / "qc" / "qc_report.json"
    try:
        last = json.loads(path.read_text(encoding="utf-8"))["rounds"][-1]
    except (OSError, ValueError, KeyError, IndexError):
        return "没有质检记录"
    if last.get("artifact_sha256") != art.get("sha256"):
        return "最近一次质检的不是当前产物（文件哈希不同）"
    if not last.get("passed"):
        return "质检记录中有 blocking 规则未通过"
    return None


def _g_qc_record_passed(task: dict, ctx: dict) -> str | None:
    return _qc_record_problem(task)


def _g_fix_left(task: dict, ctx: dict) -> str | None:
    return None if task["qc_rounds"] < ctx["max_fix_rounds"] else "自动修正次数已达上限"


def _g_fix_exhausted(task: dict, ctx: dict) -> str | None:
    return None if task["qc_rounds"] >= ctx["max_fix_rounds"] else "自动修正次数未达上限"


def self_review_problem(task: dict, ctx: dict) -> str | None:
    """发起人不能审核自己发起的任务（双方都留了姓名时判定）。"""
    by, user = (task.get("meta") or {}).get("created_by"), ctx.get("user")
    return f"不能审核自己发起的任务（发起人：{by}）" if by and user and by == user else None


def _g_approve(task: dict, ctx: dict) -> str | None:
    if (s := self_review_problem(task, ctx)):
        return s
    art = task.get("artifact") or {}
    if not art.get("sha256"):
        return "没有可交付的产物"
    if ctx.get("reviewed_sha256") != art["sha256"]:
        return "审核的版本与当前产物不一致（文件哈希不同），请刷新后重新审核"
    if (qc := _qc_record_problem(task)):
        return f"当前产物的质检未通过：{qc}"
    return None


def _g_changes(task: dict, ctx: dict) -> str | None:
    if (s := self_review_problem(task, ctx)):
        return s
    if not (ctx.get("comments") or "").strip():
        return "退回修改必须填写意见"
    if task["rework_rounds"] >= ctx["max_rework_rounds"]:
        return "返工次数已达上限"
    return None


def _g_changes_exhausted(task: dict, ctx: dict) -> str | None:
    if (s := self_review_problem(task, ctx)):
        return s
    if not (ctx.get("comments") or "").strip():
        return "退回修改必须填写意见"
    return None if task["rework_rounds"] >= ctx["max_rework_rounds"] else "返工次数未达上限"


def _g_feedback_stuck(task: dict, ctx: dict) -> str | None:
    """更正 / 撤回审核意见：仅限因意见无法落实而暂停的任务，且不能是发起人本人。"""
    if (s := self_review_problem(task, ctx)):
        return s
    return None if ctx.get("feedback_stuck") else "任务不是因审核意见无法落实而暂停"


def _g_has_files(task: dict, ctx: dict) -> str | None:
    return None if (ctx.get("files") or ctx.get("fields")) else "没有补充任何材料"


def _g_resume_to(task: dict, ctx: dict) -> str | None:
    return None if task.get("resume_to") else "没有可恢复的断点"


def _g_form_valid(task: dict, ctx: dict) -> str | None:
    return "表单校验未通过" if ctx.get("form_errors") else None


@dataclass(frozen=True)
class T:
    src: S
    event: str
    dst: S | None  # None = 回到 task.resume_to
    actors: tuple[str, ...]
    guard: Guard | None = None
    desc: str = ""


TRANSITIONS: list[T] = [
    T(S.CREATED, "submit", S.ADMISSION, (SYSTEM,), _g_form_valid, "表单 Schema 校验通过"),
    T(S.CREATED, "form_invalid", S.REJECTED, (SYSTEM,), None, "表单字段缺失或格式错误（如周期在未来、文件类型不支持）"),
    T(S.ADMISSION, "admission_pass", S.RUNNING, (SYSTEM,), None, "必需文件齐全，列、周次覆盖、数据质量合格"),
    T(S.ADMISSION, "admission_missing", S.WAITING_INPUT, (SYSTEM,), None, "缺文件 / 缺列 / 缺上周数据（记录缺什么）"),
    T(S.ADMISSION, "admission_invalid", S.REJECTED, (SYSTEM,), None, "输入不合规（含个人隐私等）"),
    T(S.ADMISSION, "step_failed", S.SUSPENDED, (SYSTEM,), None, "校验用的工具重试耗尽（保留断点）"),
    T(S.WAITING_INPUT, "supplement", S.ADMISSION, (INITIATOR,), _g_has_files, "发起人上传补充材料，重新准入"),
    T(S.RUNNING, "steps_done", S.QC, (SYSTEM,), None, "全部步骤完成"),
    T(S.RUNNING, "step_missing_input", S.WAITING_INPUT, (SYSTEM,), None, "执行中发现缺材料（保留断点）"),
    T(S.RUNNING, "step_failed", S.SUSPENDED, (SYSTEM,), None, "工具重试耗尽 / 超轮次 / 超时（保留断点）"),
    T(S.QC, "qc_pass", S.PENDING_REVIEW, (SYSTEM,), _g_qc_record_passed, "检验记录中全部 blocking 规则通过"),
    T(S.QC, "qc_fail", S.AUTO_FIXING, (SYSTEM,), _g_fix_left, "有 blocking 规则不通过，修正次数 < 上限"),
    T(S.QC, "qc_fail_exhausted", S.SUSPENDED, (SYSTEM,), _g_fix_exhausted, "修正次数 ≥ 上限，转人工"),
    T(S.QC, "step_failed", S.SUSPENDED, (SYSTEM,), None, "质检程序异常（保留断点）"),
    T(S.QC, "feedback_disputed", S.SUSPENDED, (SYSTEM,), None,
      "员工说明审核意见不适用、程序无法核实，模型修正解决不了，转审核人确认"),
    T(S.AUTO_FIXING, "fix_done", S.QC, (SYSTEM,), None, "修正后重新质检"),
    T(S.AUTO_FIXING, "step_failed", S.SUSPENDED, (SYSTEM,), None, "修正过程中工具/模型失败或超限"),
    T(S.PENDING_REVIEW, "approve", S.DELIVERED, (REVIEWER,), _g_approve, "审核人通过（不能是发起人本人），且审核版本 = 交付版本、质检通过"),
    T(S.PENDING_REVIEW, "request_changes", S.REWORK, (REVIEWER,), _g_changes, "审核人退回（不能是发起人本人），意见非空，返工次数 < 上限"),
    T(S.PENDING_REVIEW, "request_changes_exhausted", S.SUSPENDED, (REVIEWER,), _g_changes_exhausted,
      "返工次数已达上限，转线下沟通"),
    T(S.REWORK, "rework_done", S.QC, (SYSTEM,), None, "按意见重做完成，新版本重新质检"),
    T(S.REWORK, "step_missing_input", S.WAITING_INPUT, (SYSTEM,), None, "重做中发现缺材料（保留断点）"),
    T(S.REWORK, "step_failed", S.SUSPENDED, (SYSTEM,), None, "重做中工具/模型失败或超限（保留断点）"),
    T(S.SUSPENDED, "retry", None, (INITIATOR, REVIEWER), _g_resume_to, "人工确认后从断点继续（回到暂停前的状态与步骤）"),
    T(S.SUSPENDED, "revise_feedback", S.REWORK, (REVIEWER,), _g_feedback_stuck,
      "因审核意见无法落实而暂停：审核人更正意见，按新意见重做（不计返工次数，原意见留痕）"),
    T(S.SUSPENDED, "withdraw_feedback", S.QC, (REVIEWER,), _g_feedback_stuck,
      "因审核意见无法落实而暂停：审核人撤回意见，当前版本重新质检"),
    T(S.SUSPENDED, "manual_recheck", S.QC, (INITIATOR, REVIEWER), None,
      "人工补充/修改后重新质检（仍须质检通过才能送审）"),
] + [T(s, "cancel", S.CANCELLED, (INITIATOR,), None, "发起人取消") for s in S if s not in TERMINAL]


def find(src: S, event: str) -> T | None:
    for t in TRANSITIONS:
        if t.src == src and t.event == event:
            return t
    return None


def table_markdown() -> str:
    rows = ["| 当前状态 | 事件 | 闸门条件（程序判定） | 目标状态 | 触发方 |", "|---|---|---|---|---|"]
    actor = {INITIATOR: "发起人", REVIEWER: "审核人", SYSTEM: "系统"}
    cancel_done = False
    for t in TRANSITIONS:
        if t.event == "cancel":
            if cancel_done:
                continue
            cancel_done = True
            rows.append("| 任一非终态 | cancel | — | 已取消 CANCELLED | 发起人 |")
            continue
        dst = f"{LABELS[t.dst]} {t.dst}" if t.dst else "暂停前的状态（断点）"
        rows.append(f"| {LABELS[t.src]} {t.src} | {t.event} | {t.desc} | {dst} | "
                    f"{' / '.join(actor[a] for a in t.actors)} |")
    return "\n".join(rows)
