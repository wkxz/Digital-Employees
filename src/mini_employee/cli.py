"""命令行入口：mini <命令>。运行 mini -h 查看全部命令。"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .config import load_employee, runs_dir
from .orchestrator import Orchestrator
from .states import INITIATOR, LABELS, REVIEWER, S, table_markdown
from .store import IllegalTransition


def _kv(items: list[str] | None) -> dict[str, Any]:
    """key=value 列表；同一个 key 出现多次时值为列表（如多份销售明细）。"""
    out: dict[str, Any] = {}
    for it in items or []:
        k, _, v = it.partition("=")
        k, v = k.strip(), v.strip()
        out[k] = [*Orchestrator._as_list(out[k]), v] if k in out else v
    return out


def _show(o: Orchestrator, tid: str) -> None:
    t = o.store.get(tid)
    print(f"任务 {tid}  状态：{LABELS[t['state']]}（{t['state'].value}）  版本：V{t['version']}")
    if t["pause_reason"]:
        print(f"  说明：{t['pause_reason']}")
    if t["artifact"]:
        print(f"  产物：{o.task_dir(tid) / t['artifact']['primary']}")
        print(f"  哈希：{t['artifact']['sha256'][:16]}…  质检：{'通过' if t['artifact'].get('qc_passed') else '未通过'}")
    if t["state"] == S.DELIVERED:
        print(f"  交付包：{o.task_dir(tid) / 'deliverable'}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="mini", description="迷你数字员工")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("new", help="发起任务并执行")
    p.add_argument("--employee", default="weekly_report")
    p.add_argument("--period", required=True, help="统计周期，如 2026-W44")
    p.add_argument("--file", action="append", help="文件，如 sales_file=path（可多次）")
    p.add_argument("--field", action="append", help="其他表单字段，如 focus_region=华东")
    p.add_argument("--fault", action="append", help="故障注入，如 search_minutes:timeout 或 mock:direction_error")
    p.add_argument("--no-run", action="store_true")

    for name, h in (("run", "继续执行任务"), ("status", "查看任务"), ("retry", "暂停后重试（从断点继续）"),
                    ("cancel", "取消任务"), ("events", "回放事件日志")):
        q = sub.add_parser(name, help=h)
        q.add_argument("task", nargs="?" if name == "status" else None)
        q.add_argument("--as", dest="actor", default=INITIATOR)
        if name == "retry":
            q.add_argument("--clear-faults", action="store_true", help="清除该任务的故障注入（模拟网络恢复）")
        if name == "events":
            q.add_argument("--full", action="store_true")

    p = sub.add_parser("review", help="审核：通过或退回修改")
    p.add_argument("task")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--approve", action="store_true")
    g.add_argument("--changes", metavar="意见")
    p.add_argument("--as", dest="actor", default=REVIEWER)

    p = sub.add_parser("supplement", help="补充材料")
    p.add_argument("task")
    p.add_argument("--file", action="append")
    p.add_argument("--field", action="append")

    p = sub.add_parser("recheck", help="人工修改内容后重新质检")
    p.add_argument("task")
    p.add_argument("--content", help="修改后的内容 JSON 文件（省略则直接重新质检）")

    sub.add_parser("resume", help="恢复所有被中断的任务（断点恢复）")
    sub.add_parser("check-model", help="检查模型配置：连通性、工具调用、JSON 输出、响应时间")
    p = sub.add_parser("transitions", help="打印状态流转表")
    sub.add_parser("tools", help="列出工具及入参 Schema")
    p = sub.add_parser("cases", help="批量运行测试用例")
    p.add_argument("paths", nargs="*", default=["cases"])
    p.add_argument("--report", default="reports/case_report.md")
    p.add_argument("-k", dest="only", help="只运行 id 包含该字符串的用例")

    a = ap.parse_args(argv)
    o = Orchestrator() if a.cmd not in ("transitions", "tools", "cases", "check-model") else None
    try:
        if a.cmd == "new":
            faults = []
            for f in a.fault or []:
                tool, _, mode = f.partition(":")
                faults.append({"tool": tool, "mode": mode or "timeout"})
            tid = o.create(a.employee, {"period": a.period, **_kv(a.field)}, _kv(a.file), {"faults": faults})
            print(f"已创建任务 {tid}")
            if not a.no_run:
                o.drive(tid)
            _show(o, tid)
        elif a.cmd == "run":
            import fcntl
            with open(o.task_dir(a.task) / ".lock", "w") as lock:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    print("该任务正在由另一个进程执行")
                    return 3
                o.drive(a.task)
            _show(o, a.task)
        elif a.cmd == "status":
            if a.task:
                _show(o, a.task)
            else:
                for t in o.store.list():
                    print(f"{t['id']}  {LABELS[t['state']]:<6} V{t['version']}  {t['form'].get('period', '')}  "
                          f"{(t['pause_reason'] or '')[:60]}")
        elif a.cmd == "review":
            t = o.store.get(a.task)
            if a.approve:
                o.review(a.task, "approve", reviewed_sha256=(t["artifact"] or {}).get("sha256"), actor=a.actor)
            else:
                o.review(a.task, "changes", a.changes, actor=a.actor)
            _show(o, a.task)
        elif a.cmd == "supplement":
            o.supplement(a.task, _kv(a.file), _kv(a.field))
            _show(o, a.task)
        elif a.cmd == "retry":
            o.retry(a.task, a.actor, clear_faults=a.clear_faults)
            _show(o, a.task)
        elif a.cmd == "recheck":
            content = json.loads(Path(a.content).read_text(encoding="utf-8")) if a.content else None
            o.manual_recheck(a.task, content)
            _show(o, a.task)
        elif a.cmd == "cancel":
            o.cancel(a.task, a.actor)
            _show(o, a.task)
        elif a.cmd == "resume":
            res = o.resume_all()
            print("没有需要恢复的任务" if not res else "\n".join(f"{t} → {LABELS[s]}" for t, s in res))
        elif a.cmd == "events":
            for e in o.store.events(a.task):
                payload = json.dumps(e["payload"], ensure_ascii=False)
                if not a.full and len(payload) > 160:
                    payload = payload[:160] + "…"
                print(f"{e['seq']:>4} {e['ts'][11:23]} {e['type']:<20} {e['step_id'] or '':<9} {payload}")
        elif a.cmd == "check-model":
            from .llm.check import check_model
            return check_model()
        elif a.cmd == "transitions":
            print(table_markdown())
        elif a.cmd == "tools":
            from .tools import server
            from .tools.gateway import ToolGateway
            gw = ToolGateway(server.mcp, runs_dir(), lambda *x: None)
            emp = load_employee("weekly_report")
            for t in gw.model_tools(emp.tools):
                fn = t["function"]
                print(f"■ {fn['name']}：{fn['description'].splitlines()[0]}")
                print("  入参：" + json.dumps(fn["parameters"].get("properties", {}), ensure_ascii=False)[:300])
        elif a.cmd == "cases":
            from .runner import run_cases
            return run_cases(a.paths, a.report, a.only)
    except IllegalTransition as e:
        print(f"操作被拒绝：{e}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
