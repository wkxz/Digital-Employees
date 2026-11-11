"""迷你数字员工 · 控制台（新版 Web 界面）

纯标准库 HTTP 服务：提供 JSON API，并托管 web/ 下的静态页面。不引入新依赖，现场断网也能运行。
所有业务操作都调用 Orchestrator / Store 的现有方法，界面本身不做任何状态判定——能否送审、能否交付仍由程序闸门决定。

启动：uv run python app/web_server.py            （默认 http://127.0.0.1:8600，自动打开浏览器）
      uv run python app/web_server.py --port 9000 --no-browser
"""
from __future__ import annotations

import argparse
import base64
import difflib
import json
import mimetypes
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, unquote, urlparse

import jsonschema

from mini_employee import config_admin, memory
from mini_employee.config import EMPLOYEES_DIR, ROOT, load_employee, read_employee_files, runs_dir
from mini_employee.fsutil import read_json
from mini_employee.orchestrator import Orchestrator
from mini_employee.qc.rules import RULES
from mini_employee.render import anomaly_flat, build_blocks, write_md
from mini_employee.states import INITIATOR, LABELS, REVIEWER, SYSTEM, TERMINAL, TRANSITIONS, S
from mini_employee.payloads import TEXT_KINDS, FeedbackCheck
from mini_employee.placeholders import resolve
from mini_employee.store import IllegalTransition
from mini_employee.validate import ConfigError

EMP_ID = "weekly_report"
WEB_DIR = ROOT / "web"
FONT_DIR = ROOT / "assets" / "fonts"
SAMPLE = {"sales_file": ROOT / "data/official_a/销售明细.xlsx",
          "minutes_file": ROOT / "data/official_a/周会纪要_2026W44.docx"}
FAULT_OPTIONS = [
    {"id": "search_minutes:timeout", "tool": "search_minutes", "mode": "timeout", "label": "检索服务超时", "scope": "both"},
    {"id": "read_minutes:timeout", "tool": "read_minutes", "mode": "timeout", "label": "纪要读取服务超时", "scope": "both"},
    {"id": "query_kpi:error", "tool": "query_kpi", "mode": "error", "label": "KPI 查询服务 503", "scope": "global"},
    {"id": "llm:timeout", "tool": "llm", "mode": "timeout", "label": "模型接口断开", "scope": "both"},
    {"id": "mock:direction_error", "tool": "mock", "mode": "direction_error", "label": "Mock：涨跌写反", "scope": "task"},
    {"id": "mock:hardcode_number", "tool": "mock", "mode": "hardcode_number", "label": "Mock：硬编码数字", "scope": "task"},
    {"id": "mock:forbidden_tool", "tool": "mock", "mode": "forbidden_tool", "label": "Mock：越权调用工具", "scope": "task"},
    {"id": "mock:loop_tools", "tool": "mock", "mode": "loop_tools", "label": "Mock：循环调用不收尾", "scope": "task"},
]

STATIC_TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
                ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml", ".png": "image/png",
                ".json": "application/json; charset=utf-8", ".otf": "font/otf", ".woff2": "font/woff2"}

LOCK = threading.RLock()  # Store 共用一个 SQLite 连接，API 请求串行访问
_orch: Orchestrator | None = None


def orch() -> Orchestrator:
    global _orch
    if _orch is None:
        _orch = Orchestrator()
    return _orch


class ApiError(Exception):
    def __init__(self, msg: str, status: int = 400):
        super().__init__(msg)
        self.status = status


# ---------------------------------------------------------------- 工具函数

def run_bg(tid: str) -> None:
    """后台执行任务，与 Streamlit 界面相同：交给 CLI 子进程，界面不阻塞。"""
    subprocess.Popen([sys.executable, "-m", "mini_employee.cli", "run", tid], cwd=ROOT, env=os.environ.copy(),
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def save_uploads(items: list[dict[str, str]]) -> list[Path]:
    """前端以 base64 上传：[{name, data}]。保存到 runs 下的临时目录，交给 Orchestrator 复制进任务目录。"""
    out = []
    for it in items or []:
        name = Path(str(it.get("name") or "upload.bin")).name
        d = Path(tempfile.mkdtemp(dir=runs_dir()))
        p = d / name
        p.write_bytes(base64.b64decode(it.get("data") or ""))
        out.append(p)
    return out


def files_from(body: dict[str, Any], emp: Any) -> dict[str, Any]:
    paths: dict[str, Any] = {}
    multiple = {f["key"]: bool(f.get("multiple")) for f in emp.form if f["type"] == "file"}
    for key, items in (body.get("files") or {}).items():
        if key not in multiple:
            continue
        saved = save_uploads(items)
        if saved:
            paths[key] = saved if multiple[key] else saved[-1]
    return paths


def model_name() -> str:
    try:
        return os.environ.get("MINI_MODEL") or (read_employee_files(EMP_ID)[1].get("model") or {}).get("provider", "mock")
    except Exception:  # noqa: BLE001
        return "?"


def task_brief(t: dict) -> dict[str, Any]:
    return {"id": t["id"], "state": t["state"].value, "label": LABELS[t["state"]], "version": t["version"],
            "period": t["form"].get("period", ""), "pause_reason": t["pause_reason"],
            "created_at": t["created_at"], "updated_at": t["updated_at"],
            "terminal": t["state"] in TERMINAL, "has_artifact": bool(t["artifact"]),
            "qc_rounds": t["qc_rounds"], "rework_rounds": t["rework_rounds"]}


def step_progress(t: dict, emp: Any) -> list[dict[str, Any]]:
    o = orch()
    evs = o.store.events(t["id"], ["step_started", "step_completed", "step_skipped"])
    done = {e["payload"].get("key") for e in evs if e["type"] in ("step_completed", "step_skipped")}
    started = {e["payload"].get("key") for e in evs if e["type"] == "step_started"}
    out = []
    for s in emp.steps:
        key = f"{s['id']}@v{t['version']}" if s.get("per_version") else s["id"]
        status = "done" if key in done else ("running" if key in started else "todo")
        out.append({"id": s["id"], "name": s.get("name", s["id"]), "type": s.get("type", "tool"), "status": status})
    return out


def safe_task_file(tid: str, rel: str) -> Path:
    base = orch().task_dir(tid).resolve()
    p = (base / rel).resolve()
    if base not in p.parents and p != base:
        raise ApiError("路径越界", 403)
    if not p.is_file():
        raise ApiError("文件不存在", 404)
    return p


def file_url(tid: str, p: str | Path) -> str:
    base = orch().task_dir(tid).resolve()
    rel = Path(p).resolve().relative_to(base).as_posix()
    return f"/api/tasks/{tid}/file?path={quote(rel)}"


def report_blocks(t: dict, emp: Any) -> list[dict[str, Any]]:
    o = orch()
    tid = t["id"]
    d = o.task_dir(tid)
    art = t["artifact"]
    content = read_json(d / art["content_path"])
    kpis = read_json(d / "work" / "kpis.json")
    charts = {p.stem: str(p) for p in (d / "work" / "charts").glob("*.png")}
    sources = [n for v in t["meta"].get("file_names", {}).values() for n in (v if isinstance(v, list) else [v])]
    blocks, _, _ = build_blocks(content, kpis, charts, emp.report,
                                {**t["form"], "version": t["version"], "period_label": kpis["period_label"],
                                 "generated_at": "", "footer": emp.report.get("footer", ""), "sources": sources},
                                read_json(d / "work" / "minutes.json"))
    out = []
    for kind, val in blocks:
        if kind == "image":
            val = {**val, "url": file_url(tid, val["path"])}
        elif kind == "image_row":
            val = [{**x, "url": file_url(tid, x["path"])} for x in val]
        out.append({"kind": kind, "value": val})
    return out


def content_lines(t: dict, emp: Any, rel: str) -> list[str]:
    d = orch().task_dir(t["id"])
    blocks, _, _ = build_blocks(read_json(d / rel), read_json(d / "work" / "kpis.json"), {}, emp.report,
                                {**t["form"], "version": 0, "period_label": "", "generated_at": ""})
    tmp = d / ".preview.md"
    write_md(blocks, tmp)
    return tmp.read_text(encoding="utf-8").splitlines()


def kpi_digest(kpis: dict[str, Any]) -> dict[str, Any]:
    """给前端画动态图表用的精简指标（只取已算好的数，不在界面里重新计算）。"""
    flat = kpis.get("flat", {})
    tables = kpis.get("tables", {})

    def pick(rows: list[dict], keys: tuple[str, ...]) -> list[dict]:
        return [{k: r.get(k) for k in ("name", *keys)} for r in rows]

    return {
        "period": kpis.get("period"), "period_label": kpis.get("period_label"), "threshold": kpis.get("threshold"),
        "total": {k[len("total."):]: v for k, v in flat.items() if k.startswith("total.")},
        "regions": pick(tables.get("region", []), ("sales", "sales_prev", "sales_wow", "gross_margin", "delivery_share",
                                                   "target", "target_rate")),
        "categories": pick(tables.get("category", []), ("sales", "sales_wow", "share", "gross_margin")),
        "stores": pick(tables.get("store", []), ("sales", "sales_prev", "sales_wow", "region", "code", "store_type")),
        "weekly": kpis.get("weekly", []), "daily": kpis.get("daily", []), "top_items": kpis.get("top_items", []),
        "anomalies": [{k: a.get(k) for k in ("id", "label", "dimension_label", "direction", "sales", "sales_prev", "wow",
                                             "impact")} | {"drivers": [{"label": x.get("label"), "wow": x.get("wow")}
                                                                       for x in a.get("drivers") or []]}
                      for a in kpis.get("anomalies", [])],
    }


def transitions_json() -> list[dict[str, Any]]:
    return [{"src": t.src.value, "event": t.event, "dst": t.dst.value if t.dst else None, "actors": list(t.actors),
             "guard": bool(t.guard), "desc": t.desc} for t in TRANSITIONS]


# ---------------------------------------------------------------- API 处理

def api_meta() -> dict[str, Any]:
    out: dict[str, Any] = {"model": model_name(), "runs_dir": str(runs_dir()), "states": {s.value: LABELS[s] for s in S},
                           "terminal": [s.value for s in TERMINAL], "transitions": transitions_json(),
                           "faults": FAULT_OPTIONS, "roles": {INITIATOR: "发起人", REVIEWER: "审核人", SYSTEM: "系统"},
                           "sample_available": all(p.exists() for p in SAMPLE.values()),
                           "admin_configured": config_admin.admin_configured()}
    try:
        emp = load_employee(EMP_ID)
        out["employee"] = {"id": EMP_ID, "name": emp.raw.get("name", ""), "description": emp.raw.get("description", ""),
                           "form": emp.form, "steps": [{"id": s["id"], "name": s.get("name", s["id"]),
                                                       "type": s.get("type", "tool"), "tools": s.get("tools") or
                                                       ([s["tool"]] if s.get("tool") else [])} for s in emp.steps],
                           "tools": emp.tools, "limits": emp.raw.get("limits", {}),
                           "quality_checks": [{"rule": q.get("rule"),
                                               "name": q.get("name") or RULES.get(q.get("rule"), (q.get("rule"),))[0]}
                                              for q in emp.raw.get("quality_checks", []) if isinstance(q, dict)]}
        out["config_error"] = None
    except Exception as e:  # noqa: BLE001  配置写坏时只开放“员工配置”页
        out["employee"] = None
        out["config_error"] = e.errors if isinstance(e, ConfigError) else [str(e)]
    return out


def api_tasks() -> list[dict[str, Any]]:
    return [task_brief(t) for t in orch().store.list()]


def api_task(tid: str) -> dict[str, Any]:
    o = orch()
    t = o.store.get(tid)
    emp = load_employee(EMP_ID)
    out = task_brief(t)
    out["form"] = {k: v for k, v in t["form"].items() if k not in {f["key"] for f in emp.form if f["type"] == "file"}}
    out["file_names"] = t["meta"].get("file_names", {})
    out["faults"] = t["meta"].get("faults", [])
    out["steps"] = step_progress(t, emp)
    out["artifact"] = None
    if t["artifact"]:
        a = t["artifact"]
        out["artifact"] = {"tag": a.get("tag"), "sha256": a.get("sha256"), "primary": a.get("primary"),
                           "primary_url": file_url(tid, orch().task_dir(tid) / a["primary"]) if a.get("primary") else None,
                           "fix_actions": a.get("fix_actions", [])}
    dd = o.task_dir(tid) / "deliverable"
    out["deliverables"] = ([{"name": p.name, "url": file_url(tid, p), "size": p.stat().st_size}
                            for p in sorted(dd.iterdir()) if p.is_file()]
                           if t["state"] == S.DELIVERED and dd.exists() else [])
    out["resume_to"] = t.get("resume_to")
    # 因审核意见无法落实而暂停：审核人可更正或撤回意见
    out["feedback_stuck"] = o.feedback_stuck(t)
    fb = o._feedback(tid, t["version"]) if out["feedback_stuck"] else None
    out["feedback_comments"] = fb["comments"] if fb else None
    kp = o.task_dir(tid) / "work" / "kpis.json"
    out["kpis"] = kpi_digest(read_json(kp)) if kp.exists() else None
    # 跨周记忆：上一周已交付周报记下的下周关注事项与异常（撰写本周周报时交给模型跟进）
    out["last_week"] = memory.recall(o.memory, t["employee"], read_json(kp).get("prev_period")) if kp.exists() else None
    return out


def api_events(tid: str) -> list[dict[str, Any]]:
    return [{"seq": e["seq"], "ts": e["ts"], "type": e["type"], "step": e["step_id"], "payload": e["payload"]}
            for e in orch().store.events(tid)]


EDITABLE = (S.SUSPENDED, S.PENDING_REVIEW)  # 有产物时可人工在线修改报告，修改后重新质检
AN_FIELDS = ("phenomenon", "cause", "suggestion")


def clean_content(content: Any) -> dict[str, Any]:
    """网页提交的内容：去掉空段落、首尾空白；其余字段（如证据编号、对审核意见的说明）原样保留。"""
    if not isinstance(content, dict):
        raise ApiError("修改后的内容格式不正确")
    out = dict(content)
    for k, v in content.items():
        if isinstance(v, list) and all(isinstance(x, str) for x in v):
            out[k] = [x.strip() for x in v if x.strip()]
    out["anomalies"] = [{**a, **{f: str(a.get(f) or "").strip() for f in AN_FIELDS}}
                        for a in content.get("anomalies") or [] if isinstance(a, dict)]
    return out


def api_content(tid: str) -> dict[str, Any]:
    """当前版本报告的可编辑内容（占位符原文）。"""
    o = orch()
    t = o.store.get(tid)
    if not t["artifact"]:
        raise ApiError("还没有生成报告", 404)
    emp = o._emp(t)
    d = o.task_dir(tid)
    kp = d / "work" / "kpis.json"
    labels = {a["id"]: a.get("label", a["id"]) for a in read_json(kp)["anomalies"]} if kp.exists() else {}
    content = read_json(d / t["artifact"]["content_path"])
    return {"editable": S(t["state"]) in EDITABLE, "state": t["state"], "tag": t["artifact"].get("tag"),
            "sections": [{"id": s["id"], "title": s["title"], "kind": s.get("kind", "paragraphs")}
                         for s in emp.report["sections"] if s.get("kind", "paragraphs") in TEXT_KINDS],
            "anomaly_section": next((s["title"] for s in emp.report["sections"] if s.get("kind") == "anomalies"), "异常与原因"),
            "anomaly_labels": labels, "content": content}


def api_preview(tid: str, body: dict[str, Any]) -> dict[str, Any]:
    """把编辑中的内容替换占位符，供网页实时预览；返回无法识别的占位符。"""
    o = orch()
    kp = o.task_dir(tid) / "work" / "kpis.json"
    if not kp.exists():
        raise ApiError("缺少指标数据，无法预览", 404)
    flat = anomaly_flat(read_json(kp))
    missing: list[str] = []

    def r(text: Any) -> str:
        out, miss = resolve(str(text or ""), flat)
        missing.extend(miss)
        return out

    content = body.get("content") or {}
    texts = {k: [r(x) for x in v] for k, v in content.items()
             if k != "anomalies" and isinstance(v, list) and all(isinstance(x, str) for x in v)}
    anomalies = {str(a.get("id")): {f: r(a.get(f)) for f in AN_FIELDS}
                 for a in content.get("anomalies") or [] if isinstance(a, dict)}
    return {"texts": texts, "anomalies": anomalies, "missing": sorted(set(missing))}


def api_review(tid: str) -> dict[str, Any]:
    o = orch()
    t = o.store.get(tid)
    if not t["artifact"]:
        raise ApiError("还没有可审核的产物", 404)
    emp = load_employee(EMP_ID)
    d = o.task_dir(tid)
    art = t["artifact"]
    qc_json = d / "qc" / "qc_report.json"
    qc_md = d / "qc" / "质检记录.md"
    diff = None
    if art.get("prev_content_path"):
        diff = "\n".join(difflib.unified_diff(content_lines(t, emp, art["prev_content_path"]),
                                              content_lines(t, emp, art["content_path"]), "上一版", "本版",
                                              lineterm="", n=0)) or "无差异"
    fb = d / "steps" / f"feedback@v{t['version']}.json"
    return {"task": task_brief(t), "sha256": art["sha256"], "tag": art.get("tag"),
            "primary_url": file_url(tid, d / art["primary"]),
            "blocks": report_blocks(t, emp),
            "qc": read_json(qc_json) if qc_json.exists() else {"rounds": []},
            "qc_markdown": qc_md.read_text(encoding="utf-8") if qc_md.exists() else "",
            "diff": diff, "feedback": read_json(fb) if fb.exists() else None}


def api_create(body: dict[str, Any]) -> dict[str, Any]:
    if (body.get("role") or INITIATOR) != INITIATOR:
        raise ApiError("只有发起人可以发起任务，请先在右上角切换为“发起人”", 403)
    o = orch()
    emp = load_employee(EMP_ID)
    values = {k: v for k, v in (body.get("values") or {}).items() if isinstance(v, str)}
    paths = files_from(body, emp)
    if body.get("use_sample"):
        for k, p in SAMPLE.items():
            if not values.get(f"waive_{k}"):  # 发起人确认没有这份材料时不用样例补上
                paths.setdefault(k, [p] if any(f["key"] == k and f.get("multiple") for f in emp.form) else p)
    by_id = {f["id"]: f for f in FAULT_OPTIONS}
    faults = [{"tool": by_id[x]["tool"], "mode": by_id[x]["mode"]} for x in body.get("faults") or [] if x in by_id]
    tid = o.create(EMP_ID, values, paths, {"faults": faults}, user=user_of(body))
    run_bg(tid)
    return {"id": tid}


def user_of(body: dict[str, Any]) -> str | None:
    """界面填写的姓名：用于留痕和“发起人不能审核自己发起的任务”。"""
    u = body.get("user")
    return (u.strip() or None) if isinstance(u, str) else None


def api_action(tid: str, action: str, body: dict[str, Any]) -> dict[str, Any]:
    o = orch()
    role = body.get("role") or INITIATOR
    user = user_of(body)
    if role not in (INITIATOR, REVIEWER):
        raise ApiError("未知身份")
    if action == "supplement":
        emp = load_employee(EMP_ID)
        paths = files_from(body, emp)
        # 可补充的字段（统计周期等表单字段、确认没有可跳过的材料）由 orchestrator 按员工配置校验
        fields = {k: ("1" if k.startswith("waive_") else v.strip()) for k, v in (body.get("fields") or {}).items()
                  if isinstance(v, str) and v.strip()}
        try:
            o.supplement(tid, paths, fields or None, actor=role, run=False, user=user)
        except ValueError as e:
            raise ApiError(str(e)) from e
        run_bg(tid)
    elif action == "retry":
        o.retry(tid, role, clear_faults=bool(body.get("clear_faults", True)), run=False)
        run_bg(tid)
    elif action == "recheck":  # 带 content 表示人工在线修改了报告，修改后的版本重新质检
        content = body.get("content")
        if content is not None:
            content = clean_content(content)
        try:
            o.manual_recheck(tid, content, role, run=False)
        except jsonschema.ValidationError as e:
            raise ApiError(f"修改后的内容格式不正确：{e.message}") from e
        run_bg(tid)
    elif action == "cancel":
        o.cancel(tid, role)
    elif action == "review":
        if body.get("decision") == "approve":
            o.review(tid, "approve", reviewed_sha256=body.get("sha256"), actor=role, user=user)
        else:
            o.review(tid, "changes", str(body.get("comments") or ""), actor=role, run=False, user=user,
                     confirm=bool(body.get("confirm")))
            run_bg(tid)
    elif action == "revise_feedback":  # 更正审核意见；comments 为空表示撤回
        o.revise_feedback(tid, str(body.get("comments") or ""), actor=role, run=False, user=user,
                          confirm=bool(body.get("confirm")))
        run_bg(tid)
    else:
        raise ApiError(f"未知操作 {action}", 404)
    return task_brief(o.store.get(tid))


def faults_path() -> Path:
    return runs_dir() / "faults.json"


def api_faults_get() -> list[dict[str, Any]]:
    p = faults_path()
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else []


def api_faults_set(body: dict[str, Any]) -> list[dict[str, Any]]:
    if not config_admin.check_admin(body.get("admin_password")):
        raise ApiError("开关全局故障需要管理员口令" if config_admin.admin_configured()
                       else "未设置管理员口令（.env 中的 MINI_ADMIN_PASSWORD），全局故障注入不可用", 403)
    by_id = {f["id"]: f for f in FAULT_OPTIONS}
    cur = [{"tool": by_id[x]["tool"], "mode": by_id[x]["mode"]} for x in body.get("ids") or [] if x in by_id]
    faults_path().write_text(json.dumps(cur, ensure_ascii=False), encoding="utf-8")
    return cur


def api_config_get() -> dict[str, Any]:
    d = EMPLOYEES_DIR / EMP_ID
    return {"employee.yaml": (d / "employee.yaml").read_text(encoding="utf-8"),
            "SKILL.md": (d / "SKILL.md").read_text(encoding="utf-8")}


def api_config_save(body: dict[str, Any]) -> dict[str, Any]:
    """与 Streamlit 界面相同：只校验时任何人可用；保存须管理员口令，校验通过才写盘，并记录修改人、说明和改前改后全文。"""
    name, text = body.get("name"), body.get("text")
    if name not in config_admin.editable_files(EMP_ID) or not isinstance(text, str):
        raise ApiError("只能保存 employee.yaml 或 SKILL.md")
    if not body.get("save", True):
        errors, warnings = config_admin.validate_texts(EMP_ID, name, text)
        return {"saved": False, "errors": errors, "warnings": warnings, "seq": None}
    errors, warnings, seq = config_admin.save(orch().store, EMP_ID, name, text, user_of(body) or "管理员",
                                              body.get("admin_password"), str(body.get("note") or ""))
    return {"saved": seq is not None, "errors": errors, "warnings": warnings, "seq": seq}


# ---------------------------------------------------------------- HTTP

ROUTES_GET = [
    (re.compile(r"^/api/meta$"), lambda m, q: api_meta()),
    (re.compile(r"^/api/tasks$"), lambda m, q: api_tasks()),
    (re.compile(r"^/api/tasks/([\w-]+)$"), lambda m, q: api_task(m[1])),
    (re.compile(r"^/api/tasks/([\w-]+)/events$"), lambda m, q: api_events(m[1])),
    (re.compile(r"^/api/tasks/([\w-]+)/review$"), lambda m, q: api_review(m[1])),
    (re.compile(r"^/api/tasks/([\w-]+)/content$"), lambda m, q: api_content(m[1])),
    (re.compile(r"^/api/faults$"), lambda m, q: api_faults_get()),
    (re.compile(r"^/api/config$"), lambda m, q: api_config_get()),
]
ROUTES_POST = [
    (re.compile(r"^/api/tasks$"), lambda m, b: api_create(b)),
    (re.compile(r"^/api/tasks/([\w-]+)/(supplement|retry|recheck|cancel|review|revise_feedback)$"), lambda m, b: api_action(m[1], m[2], b)),
    (re.compile(r"^/api/tasks/([\w-]+)/preview$"), lambda m, b: api_preview(m[1], b)),
    (re.compile(r"^/api/faults$"), lambda m, b: api_faults_set(b)),
    (re.compile(r"^/api/config$"), lambda m, b: api_config_save(b)),
]


SSE_TICK, SSE_HEARTBEAT, SSE_MAX = 0.3, 15.0, 600.0  # 检查间隔、心跳（防代理断开空闲连接）、单次连接最长时长


def sse_frames(tid: str, ev: int, lv: int, rv: int) -> tuple[list[str], int, int, int, bool]:
    """读取任务自上次推送以来的变化，生成 SSE 帧。返回（帧, 事件游标, 实时进度游标, 行版本, 是否已结束）。
    live：节点进度与模型输出（直接推送内容）；tick：任务状态或事件有变化（页面据此立即刷新）。"""
    with LOCK:
        st = orch().store
        t = st.get(tid)
        rows = st.live(tid, lv)
        last = st.db.execute("SELECT COALESCE(MAX(seq), 0) FROM events WHERE task_id=?", (tid,)).fetchone()[0]
    frames = []
    for r in rows:
        lv = r["seq"]
        frames.append(sse_frame("live", r, f"{ev}.{lv}"))
    if last != ev or t["row_version"] != rv:
        ev, rv = last, t["row_version"]
        frames.append(sse_frame("tick", {"state": t["state"].value, "row_version": rv, "event_seq": ev}, f"{ev}.{lv}"))
    return frames, ev, lv, rv, t["state"] in TERMINAL


def sse_frame(event: str, data: Any, id_: str | None = None) -> str:
    head = f"id: {id_}\n" if id_ else ""
    return f"{head}event: {event}\ndata: {json.dumps(data, ensure_ascii=False, default=str)}\n\n"


def parse_cursor(raw: str | None) -> tuple[int, int]:
    try:
        a, b = (raw or "").split(".")
        return int(a), int(b)
    except ValueError:
        return 0, 0


class Handler(BaseHTTPRequestHandler):
    server_version = "MiniEmployee/1.0"

    def _sse(self, tid: str, q: dict[str, list[str]]) -> None:
        """任务实时推送（Server-Sent Events）。执行任务的子进程把进度写入 SQLite，这里读出变化推给浏览器；
        断线后浏览器带 Last-Event-ID 自动重连，从上次的位置继续。"""
        with LOCK:
            orch().store.get(tid)  # 任务不存在时返回 404
        ev, lv = parse_cursor(self.headers.get("Last-Event-ID") or (q.get("cursor") or [""])[0])
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Accel-Buffering", "no")  # 反向代理不要缓冲
        self.end_headers()
        rv, t0, beat = -1, time.monotonic(), time.monotonic()
        try:
            self.wfile.write(b"retry: 2000\n\n")
            while time.monotonic() - t0 < SSE_MAX:
                frames, ev, lv, rv, ended = sse_frames(tid, ev, lv, rv)
                if frames:
                    self.wfile.write("".join(frames).encode("utf-8"))
                    beat = time.monotonic()
                elif time.monotonic() - beat > SSE_HEARTBEAT:
                    self.wfile.write(b": ping\n\n")
                    beat = time.monotonic()
                self.wfile.flush()
                if ended:
                    self.wfile.write(sse_frame("end", {}).encode("utf-8"))
                    self.wfile.flush()
                    return
                time.sleep(SSE_TICK)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass  # 浏览器离开页面

    def log_message(self, fmt: str, *args: Any) -> None:  # 只打印出错的请求，保持终端干净
        if args and str(args[1])[:1] in ("4", "5"):
            sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def _send(self, status: int, body: bytes, ctype: str, extra: dict[str, str] | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, data: Any, status: int = 200) -> None:
        self._send(status, json.dumps(data, ensure_ascii=False, default=str).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _error(self, e: BaseException) -> None:
        if isinstance(e, ApiError):
            self._json({"error": str(e)}, e.status)
        elif isinstance(e, FeedbackCheck):  # 意见可能有误：交审核人确认（改意见，或确认后仍提交）
            self._json({"error": "审核意见可能有误，请确认", "kind": "feedback_check", "issues": e.issues}, 409)
        elif isinstance(e, IllegalTransition):
            self._json({"error": f"操作被拒绝：{e}", "kind": "illegal_transition"}, 409)
        elif isinstance(e, ConfigError):
            self._json({"error": "员工配置有误", "details": e.errors}, 400)
        elif isinstance(e, KeyError):
            self._json({"error": f"找不到：{e}"}, 404)
        else:
            traceback.print_exc()
            self._json({"error": f"操作失败：{type(e).__name__}: {e}"}, 500)

    def do_GET(self) -> None:  # noqa: N802
        u = urlparse(self.path)
        path = unquote(u.path)
        q = parse_qs(u.query)
        try:
            m = re.match(r"^/api/tasks/([\w-]+)/stream$", path)
            if m:
                return self._sse(m[1], q)
            m = re.match(r"^/api/tasks/([\w-]+)/file$", path)
            if m:
                with LOCK:
                    p = safe_task_file(m[1], (q.get("path") or [""])[0])
                ctype = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
                extra = {}
                if (q.get("download") or ["0"])[0] == "1":
                    extra["Content-Disposition"] = f"attachment; filename*=UTF-8''{quote(p.name)}"
                return self._send(200, p.read_bytes(), ctype, extra)
            for rx, fn in ROUTES_GET:
                m = rx.match(path)
                if m:
                    with LOCK:
                        data = fn(m, q)
                    return self._json(data)
            if path.startswith("/api/"):
                raise ApiError("接口不存在", 404)
            self._static(path)
        except Exception as e:  # noqa: BLE001
            self._error(e)

    def do_POST(self) -> None:  # noqa: N802
        path = unquote(urlparse(self.path).path)
        try:
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}") if n else {}
            for rx, fn in ROUTES_POST:
                m = rx.match(path)
                if m:
                    with LOCK:
                        data = fn(m, body)
                    return self._json(data)
            raise ApiError("接口不存在", 404)
        except Exception as e:  # noqa: BLE001
            self._error(e)

    def _static(self, path: str) -> None:
        if path.startswith("/fonts/"):
            base, rel = FONT_DIR, path[len("/fonts/"):]
        else:
            base, rel = WEB_DIR, (path.lstrip("/") or "index.html")
        p = (base / rel).resolve()
        if base.resolve() not in p.parents or not p.is_file():
            p = WEB_DIR / "index.html"  # 前端路由用 #hash，其余未知路径回到首页
        # Windows 注册表里的 MIME 映射可能有误（如 .js 变成 text/plain），常用类型直接写死
        ctype = STATIC_TYPES.get(p.suffix.lower()) or mimetypes.guess_type(p.name)[0] or "application/octet-stream"
        self._send(200, p.read_bytes(), ctype)


def main() -> None:
    ap = argparse.ArgumentParser(description="迷你数字员工 · 控制台")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8600)
    ap.add_argument("--no-browser", action="store_true")
    a = ap.parse_args()
    mimetypes.add_type("font/otf", ".otf")
    with LOCK:
        orch()
    srv = ThreadingHTTPServer((a.host, a.port), Handler)
    url = f"http://{'127.0.0.1' if a.host in ('0.0.0.0', '') else a.host}:{a.port}"
    print(f"迷你数字员工 · 控制台已启动：{url}　（Ctrl+C 退出）")
    if not a.no_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已退出")


if __name__ == "__main__":
    main()
