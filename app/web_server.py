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
import traceback
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, unquote, urlparse

import yaml

from mini_employee.config import EMPLOYEES_DIR, ROOT, load_employee, parse_skill_md, read_employee_files, runs_dir
from mini_employee.fsutil import read_json
from mini_employee.orchestrator import Orchestrator
from mini_employee.qc.rules import RULES
from mini_employee.render import build_blocks, write_md
from mini_employee.states import INITIATOR, LABELS, REVIEWER, SYSTEM, TERMINAL, TRANSITIONS, S
from mini_employee.store import IllegalTransition
from mini_employee.validate import ConfigError, validate

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
                           "sample_available": all(p.exists() for p in SAMPLE.values())}
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
    kp = o.task_dir(tid) / "work" / "kpis.json"
    out["kpis"] = kpi_digest(read_json(kp)) if kp.exists() else None
    return out


def api_events(tid: str) -> list[dict[str, Any]]:
    return [{"seq": e["seq"], "ts": e["ts"], "type": e["type"], "step": e["step_id"], "payload": e["payload"]}
            for e in orch().store.events(tid)]


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
    o = orch()
    emp = load_employee(EMP_ID)
    values = {k: v for k, v in (body.get("values") or {}).items() if isinstance(v, str)}
    paths = files_from(body, emp)
    if body.get("use_sample"):
        for k, p in SAMPLE.items():
            paths.setdefault(k, [p] if any(f["key"] == k and f.get("multiple") for f in emp.form) else p)
    by_id = {f["id"]: f for f in FAULT_OPTIONS}
    faults = [{"tool": by_id[x]["tool"], "mode": by_id[x]["mode"]} for x in body.get("faults") or [] if x in by_id]
    tid = o.create(EMP_ID, values, paths, {"faults": faults})
    run_bg(tid)
    return {"id": tid}


def api_action(tid: str, action: str, body: dict[str, Any]) -> dict[str, Any]:
    o = orch()
    role = body.get("role") or INITIATOR
    if role not in (INITIATOR, REVIEWER):
        raise ApiError("未知身份")
    if action == "supplement":
        emp = load_employee(EMP_ID)
        paths = files_from(body, emp)
        o.supplement(tid, paths, None, actor=role, run=False)
        run_bg(tid)
    elif action == "retry":
        o.retry(tid, role, clear_faults=bool(body.get("clear_faults", True)), run=False)
        run_bg(tid)
    elif action == "recheck":
        o.manual_recheck(tid, None, role, run=False)
        run_bg(tid)
    elif action == "cancel":
        o.cancel(tid, role)
    elif action == "review":
        if body.get("decision") == "approve":
            o.review(tid, "approve", reviewed_sha256=body.get("sha256"), actor=role)
        else:
            o.review(tid, "changes", str(body.get("comments") or ""), actor=role, run=False)
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
    by_id = {f["id"]: f for f in FAULT_OPTIONS}
    cur = [{"tool": by_id[x]["tool"], "mode": by_id[x]["mode"]} for x in body.get("ids") or [] if x in by_id]
    faults_path().write_text(json.dumps(cur, ensure_ascii=False), encoding="utf-8")
    return cur


def api_config_get() -> dict[str, Any]:
    d = EMPLOYEES_DIR / EMP_ID
    return {"employee.yaml": (d / "employee.yaml").read_text(encoding="utf-8"),
            "SKILL.md": (d / "SKILL.md").read_text(encoding="utf-8")}


def api_config_save(body: dict[str, Any]) -> dict[str, Any]:
    """与 Streamlit 界面相同：先校验改后的整套配置，通过了才写盘。"""
    name, text = body.get("name"), body.get("text")
    if name not in ("employee.yaml", "SKILL.md") or not isinstance(text, str):
        raise ApiError("只能保存 employee.yaml 或 SKILL.md")
    d = EMPLOYEES_DIR / EMP_ID
    try:
        is_yaml = name.endswith(".yaml")
        raw = yaml.safe_load(text if is_yaml else (d / "employee.yaml").read_text(encoding="utf-8")) or {}
        skill = (d / "SKILL.md").read_text(encoding="utf-8") if is_yaml else text
        errors, warnings = validate(raw, parse_skill_md(skill)[1])
    except Exception as e:  # noqa: BLE001  YAML 语法错误
        errors, warnings = [f"{type(e).__name__}: {e}"], []
    if not errors and body.get("save", True):
        (d / name).write_text(text, encoding="utf-8", newline="\n")
    return {"saved": not errors and body.get("save", True), "errors": errors, "warnings": warnings}


# ---------------------------------------------------------------- HTTP

ROUTES_GET = [
    (re.compile(r"^/api/meta$"), lambda m, q: api_meta()),
    (re.compile(r"^/api/tasks$"), lambda m, q: api_tasks()),
    (re.compile(r"^/api/tasks/([\w-]+)$"), lambda m, q: api_task(m[1])),
    (re.compile(r"^/api/tasks/([\w-]+)/events$"), lambda m, q: api_events(m[1])),
    (re.compile(r"^/api/tasks/([\w-]+)/review$"), lambda m, q: api_review(m[1])),
    (re.compile(r"^/api/faults$"), lambda m, q: api_faults_get()),
    (re.compile(r"^/api/config$"), lambda m, q: api_config_get()),
]
ROUTES_POST = [
    (re.compile(r"^/api/tasks$"), lambda m, b: api_create(b)),
    (re.compile(r"^/api/tasks/([\w-]+)/(supplement|retry|recheck|cancel|review)$"), lambda m, b: api_action(m[1], m[2], b)),
    (re.compile(r"^/api/faults$"), lambda m, b: api_faults_set(b)),
    (re.compile(r"^/api/config$"), lambda m, b: api_config_save(b)),
]


class Handler(BaseHTTPRequestHandler):
    server_version = "MiniEmployee/1.0"

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
