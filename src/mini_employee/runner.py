"""批量运行测试用例（YAML），输出通过率报告。

用例格式见 cases/README.md：input（表单、文件、故障注入）+ steps（依次执行的人工动作与断言）。
每条用例在独立的临时目录中运行，互不影响。
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
import traceback
from pathlib import Path
from typing import Any

import yaml

from .config import ROOT
from .fsutil import read_json
from .states import LABELS, S
from .store import IllegalTransition


class CaseFailed(AssertionError):
    pass


def _report_text(o, tid: str) -> str:
    from docx import Document

    t = o.store.get(tid)
    if not t["artifact"]:
        return ""
    p = o.task_dir(tid) / t["artifact"]["primary"]
    if p.suffix == ".docx":
        doc = Document(str(p))
        parts = [para.text for para in doc.paragraphs]
        for tb in doc.tables:
            parts += [c.text for row in tb.rows for c in row.cells]
        return "\n".join(parts)
    return p.read_text(encoding="utf-8")


def _check(o, tid: str, exp: dict[str, Any]) -> None:
    t = o.store.get(tid)
    errs = []
    if "state" in exp and t["state"] != S(exp["state"]):
        errs.append(f"状态应为 {exp['state']}，实际 {t['state']}（{t['pause_reason'] or ''}）")
    if "version" in exp and t["version"] != exp["version"]:
        errs.append(f"版本应为 {exp['version']}，实际 {t['version']}")
    if "pause_contains" in exp:
        for s in ([exp["pause_contains"]] if not isinstance(exp["pause_contains"], list) else exp["pause_contains"]):
            s = str(s)
            if s not in (t["pause_reason"] or ""):
                errs.append(f"暂停说明应包含“{s}”，实际：{t['pause_reason']}")
    if "report_contains" in exp or "report_not_contains" in exp:
        text = _report_text(o, tid)
        errs += [f"报告缺少“{s}”" for s in exp.get("report_contains", []) if s not in text]
        errs += [f"报告不应出现“{s}”" for s in exp.get("report_not_contains", []) if s in text]
    types = [e["type"] for e in o.store.events(tid)]
    errs += [f"缺少事件 {e}" for e in exp.get("events", []) if e not in types]
    for et, n in (exp.get("event_min") or {}).items():
        if types.count(et) < n:
            errs.append(f"事件 {et} 应至少 {n} 次，实际 {types.count(et)}")
    if "qc_failed_seen" in exp:
        qc = o.task_dir(tid) / "qc" / "qc_report.json"
        seen = {r["rule"] for rd in (read_json(qc)["rounds"] if qc.exists() else []) for r in rd["results"]
                if not r["passed"]}
        errs += [f"质检记录中应出现未通过规则 {r}" for r in exp["qc_failed_seen"] if r not in seen]
    if "kpis" in exp or "anomaly_labels" in exp:
        k = read_json(o.task_dir(tid) / "work" / "kpis.json")
        for key, want in (exp.get("kpis") or {}).items():
            got = k["flat"].get(key)
            tol = 0.01 if abs(want) >= 10 else 5e-5  # 金额精确到分；比率精确到 0.005 个百分点
            if got is None or abs(got - want) > tol:
                errs.append(f"KPI {key} 应为 {want}，实际 {got}")
        if "anomaly_labels" in exp and [a["label"] for a in k["anomalies"]] != exp["anomaly_labels"]:
            errs.append(f"异常应为 {exp['anomaly_labels']}，实际 {[a['label'] for a in k['anomalies']]}")
    if "state_path" in exp:
        path = [e["payload"]["to"] for e in o.store.events(tid, ["state_changed"])]
        if path != exp["state_path"]:
            errs.append(f"状态路径应为 {exp['state_path']}，实际 {path}")
    if exp.get("qc_all_blocking_passed"):
        rounds = read_json(o.task_dir(tid) / "qc" / "qc_report.json")["rounds"]
        bad = [r["rule"] for r in rounds[-1]["results"] if not r["passed"] and r["severity"] == "blocking"]
        if bad or len(rounds) != 1:
            errs.append(f"应一次质检全部通过，实际 {len(rounds)} 轮，最后未通过：{bad}")
    for sid, n in (exp.get("step_runs") or {}).items():
        runs = sum(1 for e in o.store.events(tid, ["step_started"]) if e["step_id"] == sid)
        if runs != n:
            errs.append(f"步骤 {sid} 应执行 {n} 次，实际 {runs} 次")
    for f in exp.get("deliverable_files", []):
        if not list((o.task_dir(tid) / "deliverable").glob(f)):
            errs.append(f"交付包缺少 {f}")
    if errs:
        raise CaseFailed("；".join(errs))


def _files(spec: dict[str, Any] | None) -> dict[str, Any]:
    return {k: [str(ROOT / x) for x in v] if isinstance(v, list) else str(ROOT / v) for k, v in (spec or {}).items()}


def run_case(case: dict[str, Any], workdir: Path) -> dict[str, Any]:
    os.environ["MINI_RUNS"] = str(workdir)
    os.environ["MINI_TODAY"] = "2026-11-03"
    os.environ["MINI_FAST"] = "1"
    os.environ.update({k: str(v) for k, v in (case.get("env") or {}).items()})
    from .orchestrator import Orchestrator

    o = Orchestrator()
    inp = case["input"]
    tid = o.create(inp.get("employee", "weekly_report"), {"period": inp.get("period"), **(inp.get("fields") or {})},
                   _files(inp.get("files")), {"faults": inp.get("faults", [])})
    log = [f"创建任务 {tid}"]
    crash_after = case.get("crash_after")
    if crash_after:
        env = {**os.environ, "MINI_CRASH_AFTER": crash_after}
        r = subprocess.run([sys.executable, "-m", "mini_employee.cli", "run", tid], env=env, capture_output=True,
                           text=True, cwd=ROOT)
        log.append(f"执行到“{crash_after}”后进程被强制结束（exit {r.returncode}），状态 {o.store.get(tid)['state']}")
        if r.returncode != 137:
            raise CaseFailed(f"模拟中断未生效：exit {r.returncode} {r.stderr[-300:]}")
        o = Orchestrator()
        o.resume_all()
        log.append(f"重启后断点恢复 → {o.store.get(tid)['state']}")
    else:
        o.drive(tid)
        log.append(f"执行 → {LABELS[o.store.get(tid)['state']]}")

    for step in case.get("steps", []):
        (action, arg), = step.items()
        t = o.store.get(tid)
        if action == "expect":
            _check(o, tid, arg)
            continue
        if action == "review":
            sha = arg.get("sha256", (t["artifact"] or {}).get("sha256"))
            o.review(tid, arg["decision"], arg.get("comments", ""), reviewed_sha256=sha,
                     actor=arg.get("actor", "reviewer"))
        elif action == "supplement":
            o.supplement(tid, _files(arg.get("files")), arg.get("fields"))
        elif action == "retry":
            o.retry(tid, clear_faults=arg.get("clear_faults", False))
        elif action == "recheck":
            o.manual_recheck(tid)
        elif action == "cancel":
            o.cancel(tid)
        elif action == "illegal":
            before = o.store.get(tid)["state"]
            try:
                ev = arg["event"]
                if ev == "approve":
                    o.review(tid, "approve", reviewed_sha256=arg.get("sha256", (t["artifact"] or {}).get("sha256")),
                             actor=arg.get("actor", "reviewer"))
                elif ev == "request_changes":
                    o.review(tid, "changes", arg.get("comments", ""), actor=arg.get("actor", "reviewer"))
                else:
                    o.store.transition(tid, ev, arg.get("actor", "system"), {})
            except IllegalTransition as e:
                log.append(f"非法操作 {arg} 被拒绝：{e}")
                if o.store.get(tid)["state"] != before:
                    raise CaseFailed("非法操作被拒绝但状态发生了变化") from e
                continue
            raise CaseFailed(f"非法操作 {arg} 未被拒绝")
        elif action == "compare_with_clean_run":
            clean = o.create(inp.get("employee", "weekly_report"),
                             {"period": inp.get("period"), **(inp.get("fields") or {})}, _files(inp.get("files")), {})
            o.drive(clean)
            a = read_json(o.task_dir(tid) / o.store.get(tid)["artifact"]["content_path"])
            b = read_json(o.task_dir(clean) / o.store.get(clean)["artifact"]["content_path"])
            if a != b:
                raise CaseFailed("断点恢复后的结果与一次跑完的结果不一致")
            log.append("断点恢复结果与一次跑完一致")
            continue
        else:
            raise CaseFailed(f"未知动作 {action}")
        log.append(f"{action} {arg if action != 'review' else arg.get('decision')} → {LABELS[o.store.get(tid)['state']]}")
    return {"task": tid, "log": log}


def load_cases(paths: list[str]) -> list[dict[str, Any]]:
    files: list[Path] = []
    for p in paths:
        pp = Path(p) if Path(p).is_absolute() else ROOT / p
        files += sorted(pp.glob("*.yaml")) if pp.is_dir() else [pp]
    return [{**yaml.safe_load(f.read_text(encoding="utf-8")), "_file": f.name} for f in files]


def run_cases(paths: list[str], report: str, only: str | None = None) -> int:
    cases = [c for c in load_cases(paths) if not only or only in c["id"]]
    (ROOT / "runs").mkdir(exist_ok=True)
    base = Path(tempfile.mkdtemp(prefix="cases-", dir=ROOT / "runs"))
    rows = []
    for c in cases:
        t0 = time.monotonic()
        try:
            res = run_case(c, base / c["id"])
            ok, msg, log = True, "", res["log"]
        except Exception as e:  # noqa: BLE001
            ok, msg, log = False, f"{type(e).__name__}: {e}", [traceback.format_exc(limit=3)]
        dt = time.monotonic() - t0
        rows.append({"id": c["id"], "name": c["name"], "category": c.get("category", ""), "ok": ok, "msg": msg,
                     "sec": dt, "log": log})
        print(f"{'✅' if ok else '❌'} {c['id']:<5} {c.get('category', ''):<5} {c['name']}  ({dt:.1f}s) {msg}")
    passed = sum(r["ok"] for r in rows)
    print(f"\n通过 {passed}/{len(rows)}，工作目录 {base}")
    out = Path(report) if Path(report).is_absolute() else ROOT / report
    out.parent.mkdir(parents=True, exist_ok=True)
    cats: dict[str, list[bool]] = {}
    for r in rows:
        cats.setdefault(r["category"], []).append(r["ok"])
    lines = [f"# 自测用例运行报告", "", f"运行时间：{time.strftime('%Y-%m-%d %H:%M:%S')}　模型：{os.environ.get('MINI_MODEL', '员工配置')}",
             f"", f"**通过 {passed}/{len(rows)}**", "", "| 类别 | 通过 / 总数 |", "|---|---|"]
    lines += [f"| {k} | {sum(v)}/{len(v)} |" for k, v in cats.items()]
    lines += ["", "| 用例 | 类别 | 名称 | 结果 | 耗时 | 说明 |", "|---|---|---|---|---|---|"]
    lines += [f"| {r['id']} | {r['category']} | {r['name']} | {'通过' if r['ok'] else '未通过'} | {r['sec']:.1f}s | "
              f"{r['msg'].replace('|', '/') or '；'.join(r['log'][1:])[:200]} |" for r in rows]
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"报告：{out}")
    return 0 if passed == len(rows) else 1
