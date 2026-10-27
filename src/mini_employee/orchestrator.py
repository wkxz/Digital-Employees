"""编排器（程序控制层）：驱动任务在状态机上流转。

准入、执行哪一步、能否送审、能否交付，全部由这里的确定性代码判定；模型只在 agent 步骤内被调用。
每一步的输出落盘并写 step_completed 事件（含文件哈希），中断重启后据此跳过已完成步骤。
"""
from __future__ import annotations

import json
import os
import shutil
import time
from pathlib import Path
from typing import Any

from .config import Employee, load_employee, runs_dir
from .fsutil import read_json, sha256_file, sha256_json, write_json
from .llm import LLMUnavailable, build_llm
from .llm.agent import MissingInput, StepLimitExceeded, run_agent_step
from .payloads import (PLACEHOLDER_HELP, action_items, anomaly_inputs, draft_sections, form_fields, output_schema,
                       overview, parse_feedback, placeholder_table)
from .periods import parse_week, today
from .qc.engine import append_record, blocking_failures, now, program_fix, run_rules
from .qc.rules import QCContext
from .render import anomaly_flat
from .states import AUTO, INITIATOR, REVIEWER, SYSTEM, S
from .store import IllegalTransition, Store
from .tools import server as tool_server
from .tools.errors import INTERNAL, INVALID_INPUT, MISSING_INPUT, ToolDenied, ToolFailure
from .tools.gateway import ToolGateway, load_faults
from .validate import ConfigError


class Orchestrator:
    def __init__(self, store: Store | None = None):
        self.store = store or Store()

    # ================================================================ helpers
    @staticmethod
    def task_dir(tid: str) -> Path:
        d = runs_dir() / tid
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _emit(self, tid: str):
        return lambda type_, step_id, payload: self.store.event(tid, type_, step_id, payload)

    def _gateway(self, task: dict, emp: Employee) -> ToolGateway:
        lim = emp.limits
        return ToolGateway(tool_server.mcp, self.task_dir(task["id"]), self._emit(task["id"]),
                           max_retries=lim["tool_max_retries"], backoff=lim.get("tool_backoff_seconds"),
                           timeout=lim.get("tool_timeout_seconds", 30), task_faults=task["meta"].get("faults"))

    def _out(self, tid: str, key: str) -> Any:
        return read_json(self.task_dir(tid) / "steps" / f"{key}.json")

    def _inputs_hash(self, task: dict) -> str:
        d = self.task_dir(task["id"])
        files = {v: sha256_file(d / v) for vs in task["form"].values() for v in self._as_list(vs)
                 if isinstance(v, str) and v.startswith("inputs/") and (d / v).exists()}
        return sha256_json({"form": task["form"], "files": files})

    def _done(self, tid: str, key: str, inputs_hash: str) -> bool:
        path = self.task_dir(tid) / "steps" / f"{key}.json"
        if not path.exists():
            return False
        digest = sha256_file(path)
        for e in reversed(self.store.events(tid, ["step_completed"])):
            p = e["payload"]
            if p.get("key") == key:
                return p.get("inputs_hash") == inputs_hash and p.get("sha256") == digest
        return False

    def _budget_used(self, tid: str) -> float:
        """自上次人工操作以来，模型与工具累计耗时（秒）。"""
        evs = self.store.events(tid)
        start = 0
        for i, e in enumerate(evs):
            if e["type"] == "state_changed" and e["payload"].get("actor") != SYSTEM:
                start = i
        return sum(e["payload"].get("ms", 0) for e in evs[start:]
                   if e["type"] in ("model_called", "tool_called", "tool_failed")) / 1000

    def _pause(self, tid: str, event: str, reason: str, resume_to: S | None = None, **ctx: Any) -> S:
        task = self.store.get(tid)
        return self.store.transition(tid, event, SYSTEM, {"reason": reason, **ctx}, pause_reason=reason,
                                     resume_to=(resume_to or task["state"]).value)

    # ================================================================ create / form
    @staticmethod
    def _multiple(emp: Employee, key: str) -> bool:
        return any(f["key"] == key and f.get("multiple") for f in emp.form)

    @staticmethod
    def _as_list(v: Any) -> list:
        return list(v) if isinstance(v, (list, tuple)) else ([] if v in (None, "") else [v])

    def _put_files(self, tid: str, emp: Employee, form: dict, names: dict, files: dict[str, Any], append: bool) -> None:
        """保存上传文件。multiple 字段的值是列表，补充材料时追加；其他字段直接替换。"""
        for key, src in files.items():
            saved = [self._save_input(tid, key, Path(s)) for s in self._as_list(src)]
            if not saved:
                continue
            if self._multiple(emp, key):
                keep = append and isinstance(form.get(key), list)
                form[key] = (form[key] if keep else []) + [p for p, _ in saved]
                names[key] = (names.get(key, []) if keep else []) + [n for _, n in saved]
            else:
                form[key], names[key] = saved[-1]

    def create(self, employee_id: str, values: dict[str, Any], files: dict[str, Any],
               meta: dict[str, Any] | None = None) -> str:
        emp = load_employee(employee_id)
        tid = Store.new_id()
        form = {k: v for k, v in values.items() if v not in (None, "")}
        names: dict[str, Any] = {}
        self._put_files(tid, emp, form, names, files, append=False)
        self.store.create_task(employee_id, form, {**(meta or {}), "file_names": names}, tid=tid)
        return tid

    def _save_input(self, tid: str, key: str, src: Path) -> tuple[str, str]:
        d = self.task_dir(tid) / "inputs" / key
        d.mkdir(parents=True, exist_ok=True)
        n = len(list(d.iterdir()))
        dst = d / f"{n}_{src.name}"
        if src.exists():
            shutil.copy(src, dst)
            return str(dst.relative_to(self.task_dir(tid))), src.name
        return f"missing:{src.name}", src.name  # 记录发起人声称上传、实际不存在的文件

    def validate_form(self, task: dict, emp: Employee) -> list[str]:
        errors = []
        d = self.task_dir(task["id"])
        for fld in emp.form:
            key, label, v = fld["key"], fld["label"], task["form"].get(fld["key"])
            if fld["type"] == "file":
                for one in self._as_list(v):
                    if one.startswith("missing:"):
                        continue
                    ext = Path(one).suffix.lower().lstrip(".")
                    if ext not in fld.get("ext", []):
                        errors.append(f"{label}文件类型 .{ext} 不支持，请上传 {' / '.join(fld['ext'])} 格式")
                    elif not (d / one).exists():
                        errors.append(f"{label}文件不存在")
                continue
            if v in (None, ""):
                if fld.get("required"):
                    errors.append(f"缺少必填项：{label}")
                continue
            if fld["type"] == "iso_week":
                try:
                    w = parse_week(v)
                except ValueError as e:
                    errors.append(f"{label}：{e}")
                    continue
            elif fld["type"] == "enum" and v not in fld.get("options", []):
                errors.append(f"{label}的取值“{v}”不在可选范围 {fld['options']} 内")
            elif fld["type"] == "string" and len(str(v)) > fld.get("max_length", 200):
                errors.append(f"{label}过长")
        return errors

    # ================================================================ drive
    def drive(self, tid: str, resumed: bool = False) -> S:
        for _ in range(200):
            task = self.store.get(tid)
            st = task["state"]
            if st not in AUTO:
                return st
            try:
                {S.CREATED: self._on_created, S.ADMISSION: self._on_admission, S.RUNNING: self._on_running,
                 S.REWORK: self._on_running, S.QC: self._on_qc, S.AUTO_FIXING: self._on_fixing}[st](task, resumed)
            except IllegalTransition:
                raise
            except ConfigError as e:  # 任务执行中配置被改错：暂停，改正配置后重试即可
                if st == S.CREATED:
                    raise
                self.store.event(tid, "error", None, {"state": st, "error": str(e)})
                self._pause(tid, "step_failed", f"{e}。请改正 employee.yaml / SKILL.md（可先运行 mini check-config）后重试。")
            except Exception as e:  # 兜底：任何意外都转人工，不崩溃
                if st == S.CREATED:
                    raise
                self.store.event(tid, "error", None, {"state": st, "error": f"{type(e).__name__}: {e}"})
                self._pause(tid, "step_failed", f"程序异常：{type(e).__name__}: {e}。已保留断点，可重试。")
        raise RuntimeError("状态推进次数过多，疑似循环")

    def resume_all(self) -> list[tuple[str, S]]:
        out = []
        for t in self.store.list():
            if t["state"] == S.DELIVERED and not (self.task_dir(t["id"]) / "deliverable" / "manifest.json").exists():
                self.store.event(t["id"], "task_resumed", None, {"state": t["state"], "reason": "交付包不完整，重新生成"})
                self._deliver(t["id"])  # 审核通过后、交付包写完前被中断
                out.append((t["id"], S.DELIVERED))
            elif t["state"] in AUTO:
                self.store.event(t["id"], "task_resumed", None, {"state": t["state"]})
                out.append((t["id"], self.drive(t["id"], resumed=True)))
        return out

    # ---------------------------------------------------------------- CREATED
    def _on_created(self, task: dict, resumed: bool) -> None:
        emp = load_employee(task["employee"])
        errors = self.validate_form(task, emp)
        self.store.event(task["id"], "form_validated", None, {"errors": errors})
        if errors:
            self.store.transition(task["id"], "form_invalid", SYSTEM, {"reason": "；".join(errors)},
                                  pause_reason="表单校验未通过：" + "；".join(errors))
        else:
            self.store.transition(task["id"], "submit", SYSTEM, {"form_errors": errors})

    # ---------------------------------------------------------------- ADMISSION
    MASTER_REL = "work/master.xlsx"

    def _copy_master(self, tid: str, emp: Employee) -> str | None:
        """员工配置自带的主数据（门店信息、商品信息、销售目标）复制进任务目录，工具只能读任务目录内的文件。"""
        name = (emp.raw.get("data") or {}).get("master_file")
        if not name or not (emp.dir / name).exists():
            return None
        dst = self.task_dir(tid) / self.MASTER_REL
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(emp.dir / name, dst)
        return self.MASTER_REL

    @staticmethod
    def _coverage_problem(week: Any, info: dict[str, Any]) -> str | None:
        """统计周期超出销售数据实际覆盖的日期范围：说明数据覆盖到哪天，不输出该周任何数字。
        该周只有部分日期的数据时由“计算指标”步骤列出缺少的日期并暂停。"""
        dr = info.get("date_range")
        if not dr:
            return f"销售明细中没有可识别的销售日期，无法生成统计周期 {week.key} 的周报"
        span = f"销售数据实际覆盖 {dr[0]} 至 {dr[1]}"
        not_over = "（该周尚未结束）" if week.end >= today() else ""
        if not info.get("has_current_week"):
            return (f"{span}，不包含统计周期 {week.key}（{week.start}～{week.end}）{not_over}，无法生成该周周报。"
                    "请上传包含该周的销售数据，或改选数据范围内的周期")
        return None

    def _on_admission(self, task: dict, resumed: bool) -> None:
        tid = task["id"]
        emp = load_employee(task["employee"])
        errors = self.validate_form(task, emp)
        if errors:  # 补充的材料格式不对
            self._pause(tid, "admission_missing", "补充的材料不符合要求：" + "；".join(errors), resume_to=S.RUNNING)
            return
        adm = emp.admission
        have = lambda key: [v for v in self._as_list(task["form"].get(key)) if not v.startswith("missing:")]  # noqa: E731
        missing = [f["label"] for f in emp.form if f["type"] == "file" and f.get("required") and not have(f["key"])]
        problems: list[str] = [f"缺少材料：{'、'.join(missing)}，请上传"] if missing else []
        passed: list[str] = []
        reject = adm.get("pii_policy", "reject") == "reject"
        gw = self._gateway(task, emp)
        week = parse_week(task["form"]["period"])
        info: dict[str, Any] | None = None
        minutes: dict[str, Any] | None = None
        # 已上传的材料都检查一遍，一次说清所有问题，避免发起人补一样、再被要求补另一样
        master = self._copy_master(tid, emp)
        try:
            if have("sales_file"):
                info = gw.call("read_sales_data", {
                    "file_paths": have("sales_file"), "period": task["form"]["period"],
                    "required_columns": adm["required_columns"], "column_aliases": adm["column_aliases"],
                    "max_bad_ratio": adm["max_bad_row_ratio"], "strict": False,
                    "data_config": emp.raw.get("data"), "master_file": master}, allowed=emp.tools, step_id="admission")
            if have("minutes_file"):
                try:
                    minutes = gw.call("read_minutes", {"file_path": task["form"]["minutes_file"]}, allowed=emp.tools,
                                      step_id="admission")
                except ToolFailure as e:
                    if e.kind != MISSING_INPUT:
                        raise
                    problems.append(e.message)
        except ToolFailure as e:
            if e.kind == MISSING_INPUT:
                self._pause(tid, "admission_missing", e.message + "。请补充后继续。", resume_to=S.RUNNING)
            elif e.kind == INVALID_INPUT:
                self.store.transition(tid, "admission_invalid", SYSTEM, {"reason": e.message}, pause_reason=e.message)
            elif e.kind == INTERNAL:
                self._pause(tid, "step_failed", f"准入校验时{e.tool}出错：{e.message}。已保留断点，可重试。")
            else:
                self._pause(tid, "step_failed", f"准入校验时{e.tool}不可用：{e.message}。可稍后重试。")
            return
        self.store.event(tid, "admission_checked", "admission",
                         {"sales": info, "minutes_count": (minutes or {}).get("count"), "missing": missing})
        if info is not None:
            before = len(problems)
            if info["missing_columns"]:
                hint = f"{info['missing_hint']}；" if info.get("missing_hint") else ""
                problems.append(f"销售明细缺少必需列 {info['missing_columns']}（文件实际列：{info['columns']}）。"
                                f"{hint}请补充该列后重新上传，或由管理员在配置中添加列名别名")
            elif reject and info["pii_columns"]:
                msg = f"销售明细包含个人隐私信息（列：{'、'.join(info['pii_columns'])}），按规定不能处理，请脱敏后重新发起任务"
                self.store.transition(tid, "admission_invalid", SYSTEM, {"reason": msg}, pause_reason=msg)
                return
            else:
                cover = self._coverage_problem(week, info)
                if cover:
                    problems.append(cover)
                if adm.get("require_previous_week") and info.get("has_current_week") and not info.get("has_prev_week"):
                    problems.append(f"缺少上一周（{week.prev.key}，{week.prev.start}~{week.prev.end}）的销售数据，无法计算环比。"
                                    "请补充上传（可只上传上周数据，系统会与已上传的数据自动合并）")
                c = info.get("cleaning", {})
                if c.get("bad_ratio", 0) > adm["max_bad_row_ratio"]:
                    problems.append(f"异常数据占比 {c['bad_ratio']:.1%} 超过 {adm['max_bad_row_ratio']:.0%}"
                                    f"（重复 {c['duplicates']} 行、空值 {c['null_rows']} 行、负数 {c['negative_rows']} 行），"
                                    "请核对销售明细后重新上传")
            if len(problems) == before:
                dr = info.get("date_range") or ["?", "?"]
                passed.append(f"销售明细已检查通过（{dr[0]}~{dr[1]}，{info['rows']} 行）")
        if minutes is not None:
            if reject and minutes.get("has_pii"):
                msg = "周会纪要包含个人隐私信息（手机号 / 身份证号），按规定不能处理，请脱敏后重新发起任务"
                self.store.transition(tid, "admission_invalid", SYSTEM, {"reason": msg}, pause_reason=msg)
                return
            mw = (minutes.get("meta") or {}).get("week")
            my = (minutes.get("meta") or {}).get("year")
            if mw and (mw != week.week or (my and my != week.year)):
                problems.append(f"上传的周会纪要是{f'{my} 年' if my else ''}第 {mw} 周的（{minutes['meta']['title']}），"
                                f"与统计周期 {week.key} 不一致，请上传第 {week.week} 周的周会纪要")
            else:
                passed.append(f"周会纪要已检查通过（{minutes['count']} 条）")
        if problems:
            msg = "；".join(problems) + "。" + ("".join(f"{x}。" for x in passed) if passed else "")
            self._pause(tid, "admission_missing", msg + "补充后将继续执行。", resume_to=S.RUNNING, missing=missing)
            return
        self.store.transition(tid, "admission_pass", SYSTEM, {"reason": "准入校验通过"}, resume_to=None)

    # ---------------------------------------------------------------- RUNNING / REWORK
    def _resolve(self, v: Any, task: dict, emp: Employee) -> Any:
        if isinstance(v, str) and v.startswith("$"):
            section, _, key = v[1:].partition(".")
            if section == "form":
                return task["form"].get(key)
            return emp.raw[section][key] if key else emp.raw[section]  # $data 引用整个配置段
        if isinstance(v, dict):
            return {k: self._resolve(x, task, emp) for k, x in v.items()}
        if isinstance(v, list):
            return [self._resolve(x, task, emp) for x in v]
        return v

    def _feedback(self, tid: str, version: int) -> dict[str, Any] | None:
        p = self.task_dir(tid) / "steps" / f"feedback@v{version}.json"
        return read_json(p) if p.exists() else None

    def _on_running(self, task: dict, resumed: bool) -> None:
        tid, version, state = task["id"], task["version"], task["state"]
        emp = load_employee(task["employee"])
        gw = self._gateway(task, emp)
        llm = None
        fb = self._feedback(tid, version) if state == S.REWORK else None
        rerun_sections = {t["section"] for t in fb["todos"]} if fb else set()
        ih = self._inputs_hash(task)
        steps = emp.steps
        upstream_reran = False
        render_out = None
        for idx, step in enumerate(steps, 1):
            per_v = step.get("per_version", False)
            key = f"{step['id']}@v{version}" if per_v else step["id"]
            if self._done(tid, key, ih):
                if resumed:
                    self.store.event(tid, "step_skipped", step["id"], {"key": key, "reason": "已完成，断点恢复跳过"})
                if step["type"] == "render":
                    render_out = self._out(tid, key)
                continue
            if per_v and version > 1:
                rerun_on = set(step.get("rerun_on", ["*"]))
                affected = upstream_reran or "*" in rerun_sections or bool(rerun_sections & rerun_on) or \
                    ("*" in rerun_on and bool(rerun_sections))
                prev_key = f"{step['id']}@v{version - 1}"
                if not affected and (self.task_dir(tid) / "steps" / f"{prev_key}.json").exists():
                    out = self._out(tid, prev_key)
                    digest = write_json(self.task_dir(tid) / "steps" / f"{key}.json", out)
                    self.store.event(tid, "step_completed", step["id"], {"key": key, "version": version, "sha256": digest,
                                                                          "inputs_hash": ih, "reused_from": prev_key, "ms": 0})
                    continue
            used = self._budget_used(tid)
            if used > emp.limits["max_task_seconds"]:
                self._pause(tid, "step_failed", f"任务累计用时 {used:.0f} 秒，超过上限 {emp.limits['max_task_seconds']} 秒。"
                                                f"已完成 {idx - 1}/{len(steps)} 步，重试将从第 {idx} 步继续。")
                return
            self.store.event(tid, "step_started", step["id"], {"key": key, "name": step.get("name"), "index": idx,
                                                                "total": len(steps), "version": version})
            t0 = time.monotonic()
            try:
                if step["type"] == "agent" and llm is None:
                    llm = build_llm(emp.raw.get("model", {}), load_faults(task["meta"].get("faults")))
                out = self._exec(step, task, emp, gw, llm, version, fb)
            except (MissingInput, ToolFailure) as e:
                msg = e.message if isinstance(e, ToolFailure) else str(e)
                if isinstance(e, MissingInput) or e.kind == MISSING_INPUT:
                    self._pause(tid, "step_missing_input",
                                f"第 {idx}/{len(steps)} 步“{step.get('name')}”发现缺材料：{msg}。请补充后继续。")
                else:
                    self._pause(tid, "step_failed", f"第 {idx}/{len(steps)} 步“{step.get('name')}”失败：{msg}。"
                                                    f"已完成 {idx - 1}/{len(steps)} 步，重试将从第 {idx} 步继续。")
                return
            except (StepLimitExceeded, LLMUnavailable, ToolDenied) as e:
                self._pause(tid, "step_failed", f"第 {idx}/{len(steps)} 步“{step.get('name')}”暂停：{e}。"
                                                f"已完成 {idx - 1}/{len(steps)} 步，可转人工处理或重试。")
                return
            digest = write_json(self.task_dir(tid) / "steps" / f"{key}.json", out)
            self.store.event(tid, "step_completed", step["id"], {"key": key, "version": version, "sha256": digest,
                                                                  "inputs_hash": ih,
                                                                  "ms": int((time.monotonic() - t0) * 1000)})
            if per_v:
                upstream_reran = True
            if step["type"] == "render":
                render_out = out
            if os.environ.get("MINI_CRASH_AFTER") == step["id"]:
                os._exit(137)  # 断点恢复演示：模拟进程被强制结束
        art = self._artifact(task, emp, render_out, version)
        art["prev_content_path"] = fb["prev_content_path"] if fb else None
        self.store.update(tid, artifact=art)
        self.store.transition(tid, "rework_done" if state == S.REWORK else "steps_done", SYSTEM,
                              {"reason": f"V{version} 已生成"}, resume_to=None)

    def _artifact(self, task: dict, emp: Employee, render_out: dict, version: int, tag: str | None = None) -> dict:
        primary = emp.report["output_formats"][0]
        return {"version": version, "tag": tag or f"v{version}", "files": render_out["files"],
                "primary": render_out["files"][primary]["path"], "sha256": render_out["files"][primary]["sha256"],
                "content_path": render_out["content_path"], "texts_path": render_out["texts_path"],
                "qc_passed": False}

    def _report_meta(self, task: dict, emp: Employee, version: int) -> dict[str, Any]:
        w = parse_week(task["form"]["period"])
        load = self._out(task["id"], "load")
        minutes = self._out(task["id"], "minutes")
        fields ={k: v for k, v in task["form"].items()
                  if not isinstance(v, list) and not str(v).startswith(("inputs/", "missing:"))}
        skip = set(load.get("superseded_index", []))  # 被更正版替代的销售明细（按上传顺序，同名文件也能区分）
        sources = [n for key, v in task["meta"].get("file_names", {}).items()
                   for i, n in enumerate(self._as_list(v)) if not (key == "sales_file" and i in skip)]
        return {**fields, "period": w.key, "period_compact": w.compact, "period_label": w.label, "version": version,
                "sources": sources,
                "cleaning": load.get("cleaning", {}), "alias_mapping": load.get("alias_mapping", {}),
                "overridden_rows": load.get("overridden_rows", 0), "superseded_files": load.get("superseded_files", []),
                "redacted_columns": load.get("redacted_columns", []), "minutes_masked": minutes.get("masked", 0),
                "footer": emp.report.get("footer", "")}

    def _render(self, task: dict, emp: Employee, gw: ToolGateway, content: dict, version: int, tag: str,
                step_id: str) -> dict[str, Any]:
        rel = f"work/content_{tag}.json"
        write_json(self.task_dir(task["id"]) / rel, content)
        res = gw.call("generate_report", {
            "content_path": rel, "version_tag": tag, "formats": emp.report["output_formats"],
            "report_config": emp.report, "meta": self._report_meta(task, emp, version),
            "idempotency_key": f"{task['id']}:{tag}"}, allowed=emp.tools, step_id=step_id)
        return {**res, "content_path": rel}

    def _exec(self, step: dict, task: dict, emp: Employee, gw: ToolGateway, llm: Any, version: int,
              fb: dict | None) -> dict[str, Any]:
        tid = task["id"]
        if step["type"] == "tool":
            args = self._resolve(step.get("args", {}), task, emp)
            if step["tool"] == "render_charts":
                args["idempotency_key"] = f"{tid}:{step['id']}"
            return gw.call(step["tool"], args, allowed=emp.tools, step_id=step["id"])
        if step["type"] == "render":
            draft = self._out(tid, f"draft@v{version}")
            analysis = self._out(tid, f"analysis@v{version}")
            content = {**{k: draft.get(k, []) for k in draft_sections(emp.report)},
                       "anomalies": analysis.get("anomalies", [])}
            return self._render(task, emp, gw, content, version, f"v{version}", step["id"])
        # agent
        kpis = read_json(self.task_dir(tid) / "work" / "kpis.json")
        purpose = step["purpose"]
        todos = fb["todos"] if fb else None
        fields = form_fields(task["form"], emp.form)
        if purpose == "analysis":
            prev = self._out(tid, f"analysis@v{version - 1}") if fb else None
            minutes = read_json(self.task_dir(tid) / "work" / "minutes.json")
            payload = {"period": kpis["period"], "period_label": kpis["period_label"],
                       "anomalies": anomaly_inputs(kpis), "minutes_count": len(minutes),
                       # 纪要通常很短，直接给全文，避免模型反复检索；过长时只给条数，由模型用 search_minutes 检索
                       "minutes": minutes if sum(len(m["text"]) for m in minutes) <= 8000 else None,
                       "form_fields": fields,
                       "feedback": todos, "feedback_raw": fb["comments"] if fb else None, "previous": prev}
        else:
            analysis = self._out(tid, f"analysis@v{version}")
            labels = {a["id"]: a for a in anomaly_inputs(kpis)}
            prev = self._out(tid, f"draft@v{version - 1}") if fb else None
            payload = {
                "period": kpis["period"], "period_label": kpis["period_label"],
                "focus_region": task["form"].get("focus_region"),
                "form_fields": fields,  # 表单里的全部其他字段及其写作要求（guide / section），加字段不用改代码
                "sections": [{k: v for k, v in {"id": s["id"], "title": s["title"], "guide": s.get("guide")}.items() if v}
                             for s in emp.report["sections"] if s["id"] in draft_sections(emp.report)],
                "overview": overview(kpis),
                "anomalies": [{**it, "label": labels.get(it["id"], {}).get("label", ""),
                               "direction": labels.get(it["id"], {}).get("direction", "")}
                              for it in analysis.get("anomalies", [])],
                "action_items": action_items(read_json(self.task_dir(tid) / "work" / "minutes.json")),
                "placeholders": placeholder_table(kpis),
                "placeholders_help": PLACEHOLDER_HELP,
                "feedback": todos, "feedback_raw": fb["comments"] if fb else None, "previous": prev,
            }
        lim = emp.limits
        return run_agent_step(llm, gw, self._emit(tid), step_id=step["id"], purpose=purpose,
                              system=emp.skill(*step.get("skill_sections", [])), payload=payload,
                              tools=step.get("tools", []), output_schema=output_schema(purpose, emp.report),
                              max_turns=lim["max_model_turns_per_step"], max_seconds=lim["max_step_seconds"])

    # ---------------------------------------------------------------- QC
    def _qc_context(self, task: dict, emp: Employee) -> tuple[QCContext, list[dict]]:
        d = self.task_dir(task["id"])
        art = task["artifact"]
        texts = read_json(d / art["texts_path"])
        kpis = read_json(d / "work" / "kpis.json")
        specs = list(emp.quality_checks)
        fb = self._feedback(task["id"], task["version"])
        if fb and fb["todos"]:
            specs.append({"rule": "feedback_addressed"})
        for f in form_fields(task["form"], emp.form):  # 表单字段配置了 section：程序校验该章节确实提到了填写的值
            if f.get("section") and isinstance(f["value"], str):
                specs.append({"rule": "keyword_required", "name": f"表单要求：{f['label']}",
                              "params": {"section": f["section"], "keywords": [f["value"]]}})
        prev = read_json(d / fb["prev_content_path"]) if fb and fb.get("prev_content_path") else None
        ctx = QCContext(content=read_json(d / art["content_path"]), texts=texts["sections"],
                        unresolved=texts["unresolved"], images=texts["images"], tables=texts["tables"], kpis=kpis,
                        flat=anomaly_flat(kpis), minutes=read_json(d / "work" / "minutes.json"),
                        sections=emp.report["sections"], todos=fb["todos"] if fb else [], prev_content=prev,
                        pages=texts.get("pages"))
        return ctx, specs

    def _on_qc(self, task: dict, resumed: bool) -> None:
        tid = task["id"]
        emp = load_employee(task["employee"])
        ctx, specs = self._qc_context(task, emp)
        results = run_rules(ctx, specs)
        fails = blocking_failures(results)
        qc_dir = self.task_dir(tid) / "qc"
        existing = read_json(qc_dir / "qc_report.json")["rounds"] if (qc_dir / "qc_report.json").exists() else []
        art = task["artifact"]
        record = {"round_no": len(existing) + 1, "version": task["version"], "artifact": art["tag"],
                  "artifact_sha256": art["sha256"], "ts": now(), "passed": not fails,
                  "results": [r.to_dict() for r in results], "fix_actions": art.get("fix_actions", [])}
        append_record(qc_dir, record)
        self.store.event(tid, "qc_run", None, {"round_no": record["round_no"], "passed": not fails,
                                               "failed": [r.rule for r in results if not r.passed],
                                               "blocking_failed": [r.rule for r in fails]})
        max_fix = emp.limits["qc_max_fix_rounds"]
        if not fails:
            art["qc_passed"] = True
            self.store.update(tid, artifact=art)
            self.store.transition(tid, "qc_pass", SYSTEM, {"qc_passed": True, "reason": "质检全部通过"})
        elif task["qc_rounds"] < max_fix:
            self.store.transition(tid, "qc_fail", SYSTEM, {"max_fix_rounds": max_fix,
                                                           "reason": "、".join(r.name for r in fails)})
        else:
            msgs = "；".join(d["msg"] if isinstance(d, dict) else str(d) for r in fails for d in r.details[:3])
            self.store.transition(tid, "qc_fail_exhausted", SYSTEM, {"max_fix_rounds": max_fix},
                                  pause_reason=f"自动修正 {max_fix} 轮后仍有质检项未通过：{msgs}。"
                                               "请人工修改后重新质检，或直接重试。",
                                  resume_to=S.QC.value)

    def _on_fixing(self, task: dict, resumed: bool) -> None:
        tid, version = task["id"], task["version"]
        emp = load_employee(task["employee"])
        n = task["qc_rounds"] + 1
        # 缓存键绑定“被修正的那一版产物”：断点恢复时复用；重试或人工修改后产物变了，就重新修正，不会拿旧结果覆盖
        key = f"fix@{task['artifact']['tag']}r{n}"
        ih = self._inputs_hash(task)
        if self._done(tid, key, ih):
            out = self._out(tid, key)
        else:
            self.store.event(tid, "step_started", "fix", {"key": key, "name": f"自动修正第 {n} 轮", "version": version})
            t0 = time.monotonic()
            qc = read_json(self.task_dir(tid) / "qc" / "qc_report.json")["rounds"][-1]
            fails = [r for r in qc["results"] if not r["passed"] and r["severity"] == "blocking"]
            d = self.task_dir(tid)
            content = read_json(d / task["artifact"]["content_path"])
            ctx, _ = self._qc_context(task, emp)
            words = next((c.get("params", {}).get("words", []) for c in emp.quality_checks
                          if c["rule"] == "sensitive_words"), [])
            content, actions, remaining = program_fix(content, fails, ctx.flat, words)
            gw = self._gateway(task, emp)
            if remaining:
                llm = build_llm(emp.raw.get("model", {}), load_faults(task["meta"].get("faults")))
                kpis = read_json(d / "work" / "kpis.json")
                try:
                    content = run_agent_step(
                        llm, gw, self._emit(tid), step_id="fix", purpose="fix",
                        system=emp.skill("岗位职责", "撰写周报", "注意事项") +
                        "\n\n## 质检修正\n根据 failures 中列出的问题修改 content，只改有问题的句子，其余保持不变，输出完整的 content。",
                        payload={"content": content, "failures": remaining, "overview": overview(kpis),
                                           "placeholders": placeholder_table(kpis), "placeholders_help": PLACEHOLDER_HELP},
                        tools=["query_kpi"], output_schema=output_schema("fix", emp.report),
                        max_turns=emp.limits["max_model_turns_per_step"], max_seconds=emp.limits["max_step_seconds"])
                except (StepLimitExceeded, LLMUnavailable, ToolFailure, MissingInput) as e:
                    self._pause(tid, "step_failed", f"自动修正第 {n} 轮失败：{e}。可人工修改后重新质检，或重试。")
                    return
                actions.append("模型按质检意见改写：" + "、".join(f["name"] for f in remaining))
            seq = sum(1 for e in self.store.events(tid, ["step_completed"])
                      if e["step_id"] == "fix" and e["payload"].get("version") == version) + 1
            try:
                res = self._render(task, emp, gw, content, version, f"v{version}f{seq}", "fix")
            except ToolFailure as e:
                self._pause(tid, "step_failed", f"自动修正后生成文件失败：{e.message}")
                return
            out = {"render": res, "actions": actions}
            digest = write_json(d / "steps" / f"{key}.json", out)
            self.store.event(tid, "step_completed", "fix", {"key": key, "version": version, "sha256": digest,
                                                            "inputs_hash": ih, "actions": actions,
                                                            "ms": int((time.monotonic() - t0) * 1000)})
        art = self._artifact(task, emp, out["render"], version, tag=Path(out["render"]["texts_path"]).parent.name)
        art["fix_actions"] = out["actions"]
        art["prev_content_path"] = task["artifact"].get("prev_content_path")
        self.store.update(tid, artifact=art, qc_rounds=n)
        self.store.transition(tid, "fix_done", SYSTEM, {"reason": f"第 {n} 轮修正完成"})

    # ================================================================ 人工操作
    def review(self, tid: str, decision: str, comments: str = "", reviewed_sha256: str | None = None,
               actor: str = REVIEWER, run: bool = True) -> S:
        task = self.store.get(tid)
        emp = load_employee(task["employee"])
        self.store.event(tid, "review_submitted", None, {"decision": decision, "comments": comments, "actor": actor,
                                                         "version": task["version"], "reviewed_sha256": reviewed_sha256})
        if decision == "approve":
            self.store.transition(tid, "approve", actor, {"reviewed_sha256": reviewed_sha256, "reason": "审核通过"})
            self._deliver(tid)
            return S.DELIVERED
        max_rw = emp.limits["rework_max_rounds"]
        if task["rework_rounds"] >= max_rw:
            return self.store.transition(tid, "request_changes_exhausted", actor,
                                         {"comments": comments, "max_rework_rounds": max_rw},
                                         pause_reason=f"已返工 {max_rw} 次仍未通过，请审核人与发起人线下沟通后处理",
                                         resume_to=S.PENDING_REVIEW.value)
        kpis = read_json(self.task_dir(tid) / "work" / "kpis.json")
        todos = parse_feedback(comments, emp.raw.get("feedback", {}), kpis)
        new_v = task["version"] + 1
        # 先落盘审核意见再流转：若在两步之间中断，返工时也一定读得到意见，不会原样复用上一版
        write_json(self.task_dir(tid) / "steps" / f"feedback@v{new_v}.json",
                   {"comments": comments, "todos": todos, "prev_content_path": task["artifact"]["content_path"]})
        self.store.transition(tid, "request_changes", actor, {"comments": comments, "max_rework_rounds": max_rw,
                                                              "reason": comments},
                              version=new_v, rework_rounds=task["rework_rounds"] + 1, qc_rounds=0)
        self.store.event(tid, "feedback_parsed", None, {"version": new_v, "todos": todos})
        return self.drive(tid) if run else S.REWORK

    def _deliver(self, tid: str) -> None:
        """生成交付包。可重复执行：manifest.json 最后写入，没有它就说明交付包不完整，断点恢复时会重新生成。"""
        task = self.store.get(tid)
        d = self.task_dir(tid)
        out = d / "deliverable"
        out.mkdir(exist_ok=True)
        (out / "manifest.json").unlink(missing_ok=True)
        files = []
        for fmt, f in task["artifact"]["files"].items():
            src = d / f["path"]
            shutil.copy(src, out / src.name)
            files.append({"name": src.name, "sha256": f["sha256"]})
            if fmt == "md":  # Markdown 按文件名引用同目录的图表，交付包里要带上图片
                for img in sorted(src.parent.glob("*.png")):
                    shutil.copy(img, out / img.name)
                    files.append({"name": img.name, "sha256": sha256_file(out / img.name)})
        for name in ("qc_report.json", "质检记录.md"):
            shutil.copy(d / "qc" / name, out / name)
            files.append({"name": name, "sha256": sha256_file(out / name)})
        write_json(out / "manifest.json", {"task_id": tid, "version": task["version"], "artifact": task["artifact"]["tag"],
                                           "files": files, "delivered_at": now()})
        self.store.event(tid, "delivered", None, {"dir": str(out.relative_to(d)), "files": files})

    def supplement(self, tid: str, files: dict[str, str | Path] | None = None, fields: dict[str, Any] | None = None,
                   actor: str = INITIATOR, run: bool = True) -> S:
        task = self.store.get(tid)
        emp = load_employee(task["employee"])
        form, names = dict(task["form"]), dict(task["meta"].get("file_names", {}))
        self._put_files(tid, emp, form, names, files or {}, append=True)
        form.update(fields or {})
        self.store.transition(tid, "supplement", actor, {"files": files, "fields": fields,
                                                         "reason": f"补充：{', '.join([*(files or {}), *(fields or {})])}"},
                              form=form, meta={**task["meta"], "file_names": names})
        return self.drive(tid) if run else S.ADMISSION

    def retry(self, tid: str, actor: str = INITIATOR, clear_faults: bool = False, run: bool = True) -> S:
        task = self.store.get(tid)
        updates: dict[str, Any] = {"resume_to": None}
        if task.get("resume_to") in (S.QC, S.AUTO_FIXING):
            updates["qc_rounds"] = 0
        if clear_faults:
            updates["meta"] = {**task["meta"], "faults": []}
        dst = self.store.transition(tid, "retry", actor, {"reason": "人工重试"}, **updates)
        return self.drive(tid) if run else dst

    def manual_recheck(self, tid: str, content: dict[str, Any] | None = None, actor: str = INITIATOR,
                       run: bool = True) -> S:
        """人工修改内容（可选）后重新质检；仍须质检通过才能送审。"""
        import jsonschema

        task = self.store.get(tid)
        emp = load_employee(task["employee"])
        updates: dict[str, Any] = {"qc_rounds": 0, "resume_to": None}
        if content is not None:
            jsonschema.validate(content, output_schema("fix", emp.report))
            n = len(list((self.task_dir(tid) / "work").glob(f"content_v{task['version']}m*.json"))) + 1
            tag = f"v{task['version']}m{n}"
            res = self._render(task, emp, self._gateway(task, emp), content, task["version"], tag, "manual")
            art = self._artifact(task, emp, res, task["version"], tag=tag)
            art["fix_actions"] = ["人工修改内容"]
            art["prev_content_path"] = (task["artifact"] or {}).get("prev_content_path")
            updates["artifact"] = art
        elif not task.get("artifact"):
            raise IllegalTransition("还没有可质检的产物，请使用重试")
        self.store.transition(tid, "manual_recheck", actor, {"reason": "人工处理后重新质检"}, **updates)
        return self.drive(tid) if run else S.QC

    def cancel(self, tid: str, actor: str = INITIATOR) -> S:
        return self.store.transition(tid, "cancel", actor, {"reason": "发起人取消"})

    # ================================================================ 查询
    def summary(self, tid: str) -> dict[str, Any]:
        t = self.store.get(tid)
        return {"id": t["id"], "state": t["state"].value, "version": t["version"], "pause_reason": t["pause_reason"],
                "artifact": t["artifact"], "form": t["form"]}


def task_json(t: dict) -> str:
    return json.dumps(t, ensure_ascii=False, indent=2, default=str)
