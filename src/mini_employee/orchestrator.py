"""编排器（程序控制层）：驱动任务在状态机上流转。

任务推进由 LangGraph 编排图执行（见 workflow.py），图的每个节点调用这里的状态处理函数。
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

from . import config_admin
from .config import Employee, load_employee, runs_dir
from .fsutil import read_json, sha256_file, sha256_json, write_json
from .llm import LLMUnavailable, build_llm
from .llm.agent import MissingInput, StepLimitExceeded, run_agent_step
from .payloads import (PLACEHOLDER_HELP, FeedbackCheck, check_feedback, action_items, anomaly_inputs, draft_sections, form_fields, output_schema,
                       overview, parse_feedback, placeholder_table)
from .periods import parse_week, today
from .qc.engine import append_record, blocking_failures, now, program_fix, run_rules
from .qc import review as cause_review
from .qc.rules import QCContext
from .render import anomaly_flat
from .states import AUTO, INITIATOR, REVIEWER, SYSTEM, S, self_review_problem
from .store import IllegalTransition, Store
from .tools import server as tool_server
from .tools.errors import INTERNAL, INVALID_INPUT, MISSING_INPUT, ToolDenied, ToolFailure
from .tools.gateway import ToolGateway, load_faults
from .validate import FIX_DEFAULT, FIX_SKILL_SECTIONS, ConfigError, _refs
from .workflow import StepRun, build_task_graph, open_checkpointer, run_steps, run_task


class Orchestrator:
    def __init__(self, store: Store | None = None):
        self.store = store or Store()
        self._graph: Any = None

    # ================================================================ helpers
    @staticmethod
    def task_dir(tid: str) -> Path:
        d = runs_dir() / tid
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _emp(self, task: dict) -> Employee:
        """任务按发起时的配置快照执行；没有快照的旧任务读当前配置。"""
        base = self.task_dir(task["id"]) / "config"
        return load_employee(task["employee"], base if (base / task["employee"]).exists() else None)

    def _emit(self, tid: str):
        return lambda type_, step_id, payload: self.store.event(tid, type_, step_id, payload)

    def _gateway(self, task: dict, emp: Employee) -> ToolGateway:
        lim = emp.limits
        return ToolGateway(tool_server.mcp, self.task_dir(task["id"]), self._emit(task["id"]),
                           max_retries=lim["tool_max_retries"], backoff=lim.get("tool_backoff_seconds"),
                           timeout=lim.get("tool_timeout_seconds", 30), task_faults=task["meta"].get("faults"))

    def _out(self, tid: str, key: str) -> Any:
        return read_json(self.task_dir(tid) / "steps" / f"{key}.json")

    def _inputs_hash(self, task: dict, keys: set[str] | None = None) -> str:
        """输入哈希：keys 为 None 时覆盖整张表单；否则只覆盖这些表单字段（及其文件内容）。"""
        d = self.task_dir(task["id"])
        form = task["form"] if keys is None else {k: v for k, v in task["form"].items() if k in keys}
        files = {v: sha256_file(d / v) for vs in form.values() for v in self._as_list(vs)
                 if isinstance(v, str) and v.startswith("inputs/") and (d / v).exists()}
        return sha256_json({"form": form, "files": files})

    def _step_hashes(self, task: dict, emp: Employee, anc: dict[str, set[str]]) -> dict[str, str]:
        """每个步骤的输入哈希。程序步骤只覆盖它和上游步骤引用的表单字段：补充销售明细时，
        只依赖周会纪要的步骤不必重做；模型与生成步骤会用到全部表单字段，覆盖整张表单。"""
        own = {s["id"]: {r[6:] for r in _refs([s.get("args", {}), s.get("skip_if_empty")]) if r.startswith("$form.")}
               for s in emp.steps}
        full = self._inputs_hash(task)
        return {s["id"]: self._inputs_hash(task, own[s["id"]].union(*(own[a] for a in anc[s["id"]])))
                if s["type"] == "tool" else full for s in emp.steps}

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
               meta: dict[str, Any] | None = None, user: str | None = None) -> str:
        load_employee(employee_id)  # 当前配置有错时直接报错，不创建任务
        tid = Store.new_id()
        # 锁定配置：复制一份到任务目录，之后有人改配置也不影响这个任务
        sha = config_admin.snapshot(self.store, employee_id, self.task_dir(tid))
        emp = load_employee(employee_id, self.task_dir(tid) / "config")
        form = self._normalize(emp, {k: v for k, v in values.items() if v not in (None, "")})
        names: dict[str, Any] = {}
        self._put_files(tid, emp, form, names, files, append=False)
        self.store.create_task(employee_id, form, {**(meta or {}), "file_names": names, "created_by": user,
                                                   "config_sha": sha}, tid=tid)
        self.store.event(tid, "config_snapshot", None, {"employee": employee_id, "config_sha": sha})
        return tid

    @staticmethod
    def _normalize(emp: Employee, form: dict[str, Any]) -> dict[str, Any]:
        """周期类字段能解析的改成规范写法；解析不了的保持原样，由表单校验给出原因。"""
        for f in emp.form:
            if f["type"] == "iso_week" and isinstance(form.get(f["key"]), str):
                try:
                    form[f["key"]] = parse_week(form[f["key"]]).key
                except ValueError:
                    pass
        return form

    @staticmethod
    def waived(form: dict[str, Any], fld: dict[str, Any]) -> bool:
        """发起人明确确认没有这份材料（只对配置了 waivable 的必填文件生效）：表单值 waive_<key> 为真。"""
        return bool(fld.get("waivable")) and str(form.get(f"waive_{fld['key']}") or "").lower() in ("1", "true", "yes", "是")

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
    @property
    def graph(self) -> Any:
        """任务编排图（LangGraph），检查点存于任务库旁的 graph_checkpoints.sqlite。"""
        if self._graph is None:
            self._graph = build_task_graph(self, open_checkpointer(Path(self.store.path).parent / "graph_checkpoints.sqlite"))
        return self._graph

    def drive(self, tid: str, resumed: bool = False) -> S:
        """推进任务直到等人处理或结束：交给编排图执行，返回 SQLite 中的最新状态。"""
        run_task(self.graph, tid, resumed, self.store)
        return S(self.store.get(tid)["state"])

    def advance(self, tid: str, resumed: bool = False) -> None:
        """编排图的自动节点：执行当前状态的处理函数一次；任何异常都转人工，不崩溃。"""
        task = self.store.get(tid)
        st = task["state"]
        if st not in AUTO:
            return
        try:
            {S.CREATED: self._on_created, S.ADMISSION: self._on_admission, S.RUNNING: self._on_running,
             S.REWORK: self._on_running, S.QC: self._on_qc, S.AUTO_FIXING: self._on_fixing}[st](task, resumed)
        except IllegalTransition:
            raise
        except ConfigError as e:  # 任务执行中配置被改错：暂停，改正配置后重试即可
            if st == S.CREATED:
                raise
            self.store.event(tid, "error", None, {"state": st, "error": str(e)})
            self._pause(tid, "step_failed", f"{e}。请改正 employee.yaml / SKILL.md（可先运行 mini check-config）后，"
                                            "选择“使用最新配置”重试。")
        except Exception as e:  # 兜底：任何意外都转人工，不崩溃
            if st == S.CREATED:
                raise
            self.store.event(tid, "error", None, {"state": st, "error": f"{type(e).__name__}: {e}"})
            self._pause(tid, "step_failed", f"程序异常：{type(e).__name__}: {e}。已保留断点，可重试。")

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
        emp = self._emp(task)
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

    UPLOAD_MASTER = "请在“主数据”一栏上传包含最新门店信息、商品信息、销售目标工作表的 xlsx（或上传带这些工作表的销售明细 xlsx）后继续"

    @classmethod
    def _master_gap(cls, week: Any, info: dict[str, Any]) -> str | None:
        """本周或上周有销售、但主数据里找不到的门店 / 商品编码（新开门店、新上市商品），以及本周没有销售目标。
        前者关联不到区域 / 品类，会被当作空值剔除、悄悄少算；后者算不出目标达成率。都暂停，请发起人补主数据。"""
        weeks = {week.key, week.prev.key}
        gaps = []
        for sheet, codes in (info.get("unmatched_rows") or {}).items():
            hit = {c: v for c, v in codes.items() if weeks & set(v["weeks"])}
            if hit:
                desc = "、".join(f"{c}（{v['rows']} 行" + (f"、实收 {v['sales']:,.0f} 元" if v.get("sales") is not None else "")
                                + "）" for c, v in hit.items())
                gaps.append(f"销售明细中的编码 {desc}在{sheet}中找不到")
        if gaps:
            gaps = ["；".join(gaps) + "，这部分销售额无法归入区域或品类，直接计算会少算"]
        tw = info.get("target_weeks")
        if tw and f"W{week.week:02d}" not in tw and f"W{week.week}" not in tw:
            gaps.append(f"销售目标中没有第 {week.week} 周的目标（现有 {tw[0]}～{tw[-1]}），无法计算目标达成率")
        return "；".join(gaps) + "。" + cls.UPLOAD_MASTER if gaps else None

    def _on_admission(self, task: dict, resumed: bool) -> None:
        tid = task["id"]
        emp = self._emp(task)
        errors = self.validate_form(task, emp)
        if errors:  # 补充的材料格式不对
            self._pause(tid, "admission_missing", "补充的材料不符合要求：" + "；".join(errors), resume_to=S.RUNNING)
            return
        adm = emp.admission
        have = lambda key: [v for v in self._as_list(task["form"].get(key)) if not v.startswith("missing:")]  # noqa: E731
        absent = [f for f in emp.form if f["type"] == "file" and f.get("required") and not have(f["key"])]
        waived = [f for f in absent if self.waived(task["form"], f)]
        missing = [f["label"] for f in absent if f not in waived]
        problems: list[str] = [f"缺少材料：{'、'.join(missing)}，请上传"] if missing else []
        if missing and any(f.get("waivable") for f in absent):
            problems[0] += "（" + "；".join(f"如{f['waivable']}，可勾选后继续" for f in absent if f.get("waivable")) + "）"
        if waived and not resumed:
            self.store.event(tid, "material_waived", "admission",
                             {"fields": [f["label"] for f in waived], "by": task["meta"].get("waived_by")
                              or task["meta"].get("created_by"), "reason": "发起人确认没有该材料，继续执行"})
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
                    "data_config": emp.raw.get("data"), "master_file": master,
                    "master_upload": (have("master_file") or [None])[0]}, allowed=emp.tools, step_id="admission")
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
                master_gap = self._master_gap(week, info)
                if master_gap:
                    problems.append(master_gap)
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
                got = f"{f'{my} 年' if my else ''}第 {mw} 周"
                msg = (f"上传的周会纪要是{got}的（{minutes['meta']['title']}），与统计周期 {week.key}"
                       f"（{week.year} 年第 {week.week} 周）不一致")
                if my and mw == week.week and my != week.year:
                    msg += (f"。纪要与统计周期周号相同、年份不同，请先确认统计周期的年份是否填错"
                            f"（如应为 {my}-W{mw:02d}，可在补充材料处直接修改统计周期）")
                else:
                    msg += f"，请上传 {week.year} 年第 {week.week} 周的周会纪要，或修改统计周期"
                problems.append(msg)
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
        """执行 / 返工：按员工配置的步骤依赖构建步骤图（LangGraph），没有依赖关系的步骤并行执行。"""
        emp = self._emp(task)
        fb = self._feedback(task["id"], task["version"]) if task["state"] == S.REWORK else None
        run = StepRun(task=task, emp=emp, fb=fb, resumed=resumed,
                      rerun_sections={t["section"] for t in fb["todos"]} if fb else set())
        run.ih = self._step_hashes(task, emp, run.ancestors)
        run_steps(self, run)

    def _key(self, run: "StepRun", step: dict) -> str:
        return f"{step['id']}@v{run.task['version']}" if step.get("per_version") else step["id"]

    def run_step(self, run: "StepRun", step: dict, reran: set[str]) -> dict[str, Any]:
        """步骤图的一个节点：执行一个步骤。失败时不在这里改状态，只返回失败信息，
        由汇合节点在同一批并行步骤都结束后统一暂停（避免并行分支同时改状态）。"""
        task, emp = run.task, run.emp
        tid, version = task["id"], task["version"]
        steps = emp.steps
        idx = next(i for i, s in enumerate(steps, 1) if s["id"] == step["id"])
        per_v = step.get("per_version", False)
        key = self._key(run, step)
        ih = run.ih[step["id"]]
        if self._done(tid, key, ih):
            if run.resumed:
                self.store.event(tid, "step_skipped", step["id"], {"key": key, "reason": "已完成，断点恢复跳过"})
            return {}
        if per_v and version > 1:
            rerun_on = set(step.get("rerun_on", ["*"]))
            upstream_reran = bool(reran & run.ancestors[step["id"]])
            affected = upstream_reran or "*" in run.rerun_sections or bool(run.rerun_sections & rerun_on) or \
                ("*" in rerun_on and bool(run.rerun_sections))
            prev_key = f"{step['id']}@v{version - 1}"
            if not affected and (self.task_dir(tid) / "steps" / f"{prev_key}.json").exists():
                out = self._out(tid, prev_key)
                digest = write_json(self.task_dir(tid) / "steps" / f"{key}.json", out)
                self.store.event(tid, "step_completed", step["id"], {"key": key, "version": version, "sha256": digest,
                                                                      "inputs_hash": ih, "reused_from": prev_key, "ms": 0})
                return {}
        used = self._budget_used(tid)
        if used > emp.limits["max_task_seconds"]:
            return {"failure": {"event": "step_failed", "idx": idx, "kind": "budget",
                                "msg": f"任务累计用时 {used:.0f} 秒，超过上限 {emp.limits['max_task_seconds']} 秒"}}
        self.store.event(tid, "step_started", step["id"], {"key": key, "name": step.get("name"), "index": idx,
                                                            "total": len(steps), "version": version})
        t0 = time.monotonic()
        try:
            llm = build_llm(emp.raw.get("model", {}), load_faults(task["meta"].get("faults"))) \
                if step["type"] == "agent" else None
            out = self._exec(step, task, emp, self._gateway(task, emp), llm, version, run.fb)
        except (MissingInput, ToolFailure) as e:
            msg = e.message if isinstance(e, ToolFailure) else str(e)
            missing = isinstance(e, MissingInput) or e.kind == MISSING_INPUT
            return {"failure": {"event": "step_missing_input" if missing else "step_failed", "idx": idx,
                                "kind": "missing" if missing else "failed", "msg": msg}}
        except (StepLimitExceeded, LLMUnavailable, ToolDenied) as e:
            return {"failure": {"event": "step_failed", "idx": idx, "kind": "limit", "msg": str(e)}}
        digest = write_json(self.task_dir(tid) / "steps" / f"{key}.json", out)
        self.store.event(tid, "step_completed", step["id"], {"key": key, "version": version, "sha256": digest,
                                                              "inputs_hash": ih,
                                                              "ms": int((time.monotonic() - t0) * 1000)})
        if os.environ.get("MINI_CRASH_AFTER") == step["id"]:
            os._exit(137)  # 断点恢复演示：模拟进程被强制结束
        return {"reran": [step["id"]]} if per_v else {}

    def finish_steps(self, run: "StepRun", failure: dict[str, Any] | None) -> None:
        """步骤图的汇合节点：全部步骤完成则生成产物并流转；有步骤失败则暂停并说明进度。"""
        task, emp = run.task, run.emp
        tid, version, state = task["id"], task["version"], task["state"]
        steps = emp.steps
        if failure:
            done = sum(1 for s in steps if self._done(tid, self._key(run, s), run.ih[s["id"]]))
            idx, msg, name = failure["idx"], failure["msg"], steps[failure["idx"] - 1].get("name")
            total = len(steps)
            if failure["kind"] == "missing":
                reason = f"第 {idx}/{total} 步“{name}”发现缺材料：{msg}。请补充后继续。"
            elif failure["kind"] == "budget":
                reason = f"{msg}。已完成 {done}/{total} 步，重试将从第 {idx} 步继续。"
            elif failure["kind"] == "limit":
                reason = f"第 {idx}/{total} 步“{name}”暂停：{msg}。已完成 {done}/{total} 步，可转人工处理或重试。"
            else:
                reason = f"第 {idx}/{total} 步“{name}”失败：{msg}。已完成 {done}/{total} 步，重试将从第 {idx} 步继续。"
            self._pause(tid, failure["event"], reason)
            return
        render_step = next(s for s in steps if s["type"] == "render")
        art = self._artifact(task, emp, self._out(tid, self._key(run, render_step)), version)
        art["prev_content_path"] = run.fb["prev_content_path"] if run.fb else None
        self.store.update(tid, artifact=art)
        self.store.transition(tid, "rework_done" if state == S.REWORK else "steps_done", SYSTEM,
                              {"reason": f"V{version} 已生成"}, resume_to=None)

    def _artifact(self, task: dict, emp: Employee, render_out: dict, version: int, tag: str | None = None) -> dict:
        primary = emp.report["output_formats"][0]
        return {"version": version, "tag": tag or f"v{version}", "files": render_out["files"],
                "primary": render_out["files"][primary]["path"], "sha256": render_out["files"][primary]["sha256"],
                "content_path": render_out["content_path"], "texts_path": render_out["texts_path"],
                "qc_passed": False}

    def _skip_optional(self, task: dict, step: dict) -> dict[str, Any]:
        """可选材料未上传（如周会纪要）：不调用工具，写一份空结果，后续步骤按“没有这份材料”处理。"""
        if step["tool"] == "read_minutes":
            write_json(self.task_dir(task["id"]) / "work" / "minutes.json", [])
            return {"count": 0, "paragraphs": [], "has_pii": False, "masked": 0, "meta": {}, "skipped": "发起人确认本周没有周会纪要"}
        return {"skipped": f"未提供 {step['skip_if_empty']}"}

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
                "minutes_missing": bool(minutes.get("skipped")),
                "master_origin": load.get("master_origin", {}),
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
            if step.get("skip_if_empty") and not self._resolve(step["skip_if_empty"], task, emp):
                return self._skip_optional(task, step)
            args = self._resolve(step.get("args", {}), task, emp)
            if step["tool"] == "render_charts":
                args["idempotency_key"] = f"{tid}:{step['id']}"
            return gw.call(step["tool"], args, allowed=emp.tools, step_id=step["id"])
        if step["type"] == "render":
            draft = self._out(tid, f"draft@v{version}")
            analysis = self._out(tid, f"analysis@v{version}")
            content = {**{k: draft.get(k, []) for k in draft_sections(emp.report)},
                       "anomalies": analysis.get("anomalies", [])}
            # 分析、撰写两步都可能说明同一条意见：按意见去重，撰写步骤的说明在后、优先
            resp = list({r.get("instruction"): r for r in (analysis.get("feedback_response") or [])
                         + (draft.get("feedback_response") or []) if isinstance(r, dict)}.values())
            if resp:  # 员工对审核意见的说明（如“不适用”及理由），质检时由程序核实
                content["feedback_response"] = resp
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
        emp = self._emp(task)
        ctx, specs = self._qc_context(task, emp)
        if any(c["rule"] == "cause_review" for c in specs):
            self._review_causes(task, emp, ctx)
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
        disputed = [d for r in fails for d in r.details if isinstance(d, dict) and d.get("type") == "feedback_disputed"]
        if fails and all(isinstance(d, dict) and d.get("type") in ("feedback_disputed", "note")
                         for r in fails for d in r.details):
            msg = "；".join(d["msg"] for d in disputed)
            self.store.transition(tid, "feedback_disputed", SYSTEM, {"reason": msg},
                                  pause_reason=f"{msg}。请审核人确认：可更正或撤回该意见（任务页“更正审核意见”），或线下沟通后处理。",
                                  resume_to=S.QC.value)
            return
        if not fails:
            art["qc_passed"] = True
            self.store.update(tid, artifact=art)
            self.store.transition(tid, "qc_pass", SYSTEM, {"qc_passed": True, "reason": "质检全部通过"})
        elif task["qc_rounds"] < max_fix:
            self.store.transition(tid, "qc_fail", SYSTEM, {"max_fix_rounds": max_fix,
                                                           "reason": "、".join(r.name for r in fails)})
        else:
            msgs = "；".join(d["msg"] if isinstance(d, dict) else str(d) for r in fails for d in [x for x in r.details if not (isinstance(x, dict) and x.get("type") == "note")][:3])
            self.store.transition(tid, "qc_fail_exhausted", SYSTEM, {"max_fix_rounds": max_fix},
                                  pause_reason=f"自动修正 {max_fix} 轮后仍有质检项未通过：{msgs}。"
                                               "请人工修改后重新质检，或直接重试。",
                                  resume_to=S.QC.value)

    def _review_causes(self, task: dict, emp: Employee, ctx: QCContext) -> None:
        """复核员：另一个模型逐条核对原因与所引纪要是否相符，结论写入 ctx 交给质检规则 cause_review。
        结论按条目缓存；复核员不可用时不阻塞任务，只在检验记录里说明。"""
        tid = task["id"]
        items = cause_review.review_items(ctx.content, ctx.minutes, ctx.kpis)

        def run(todo: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], str]:
            llm = build_llm(emp.raw.get("model", {}), load_faults(task["meta"].get("faults")), role="reviewer")
            out = run_agent_step(llm, self._gateway(task, emp), self._emit(tid), step_id="verify", purpose="verify",
                                 system=cause_review.SYSTEM, payload={"items": todo}, tools=[],
                                 output_schema=cause_review.SCHEMA, max_turns=3,
                                 max_seconds=emp.limits["max_step_seconds"])
            return out["verdicts"], llm.name

        try:
            ctx.reviews = cause_review.review_causes(items, self.task_dir(tid) / "work" / "cause_review", run)
        except (StepLimitExceeded, LLMUnavailable, ToolFailure, MissingInput) as e:
            ctx.review_note = f"复核员未能完成复核（{e}），本轮未复核，不影响其他质检项"

    def _must_keep(self, specs: list[dict[str, Any]], task: dict) -> list[str]:
        """修正时必须同时满足的其他质检要求（来自本任务的质检配置），避免修一处、坏一处
        （如压缩篇幅时删掉摘要里的“目标达成率”）。"""
        out = []
        for c in specs:
            r, p = c["rule"], c.get("params") or {}
            if r == "keyword_required":
                where = f"{p['section']} 章节" if p.get("section") else "正文"
                out.append(f"{where}必须提到：{'、'.join(p.get('keywords', []))}")
            elif r == "regex_forbidden":
                out.append(f"不得出现：{'、'.join(p.get('patterns', []))}")
            elif r == "cause_evidence":
                out.append("每条异常都要有原因；有纪要依据的保留纪要引用，没有的写“原因待核实”")
            elif r == "cause_review" and c.get("severity", "blocking") == "blocking":
                out.append("复核员认为依据不足的原因：改引真正支持它的纪要条目，或改写为“原因待核实”")
            elif r == "threshold_coverage":
                out.append("环比超过阈值的区域、门店必须仍出现在异常章节，不能为压缩篇幅删掉异常条目")
            elif r == "data_consistency":
                out.append("数字只用占位符，不自己写数字；涨跌词后的占位符加 |abs")
            elif r == "length":
                out.append(f"summary 不超过 {p.get('summary_max_items', 5)} 条、{p.get('summary_max_chars', 400)} 字")
            elif r == "feedback_addressed":
                fb = self._feedback(task["id"], task["version"])
                out.append(f"已落实的审核意见不能改回：{fb['comments']}" if fb else "已落实的审核意见不能改回")
        return out

    def _on_fixing(self, task: dict, resumed: bool) -> None:
        tid, version = task["id"], task["version"]
        emp = self._emp(task)
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
            ctx, specs = self._qc_context(task, emp)
            words = next((c.get("params", {}).get("words", []) for c in emp.quality_checks
                          if c["rule"] == "sensitive_words"), [])
            old_resp = content.get("feedback_response")
            content, actions, remaining = program_fix(content, fails, ctx.flat, words)
            gw = self._gateway(task, emp)
            if remaining and task["artifact"].get("manual"):
                # 人工修改的版本：只做程序能确定的修正，不让模型改写，避免覆盖人工修改；交回人工继续修改
                msgs = "；".join(d["msg"] if isinstance(d, dict) else str(d) for r in remaining
                                for d in [x for x in r.get("details", []) if not (isinstance(x, dict) and x.get("type") == "note")][:3])
                names = "、".join(r["name"] for r in remaining)
                self._pause(tid, "step_failed", f"人工修改后的版本质检未通过（{names}）：{msgs or names}。"
                            "为避免覆盖人工修改，不调用模型改写；请继续修改后重新提交质检。", resume_to=S.QC)
                return
            if remaining:
                llm = build_llm(emp.raw.get("model", {}), load_faults(task["meta"].get("faults")))
                kpis = read_json(d / "work" / "kpis.json")
                try:
                    content = run_agent_step(
                        llm, gw, self._emit(tid), step_id="fix", purpose="fix",
                        system=emp.skill(*FIX_SKILL_SECTIONS) +
                        ("" if "质检修正" in emp.skill_sections else f"\n\n## 质检修正\n{FIX_DEFAULT}"),
                        payload={"content": content, "failures": remaining, "must_keep": self._must_keep(specs, task),
                                 "overview": overview(kpis),
                                           "placeholders": placeholder_table(kpis), "placeholders_help": PLACEHOLDER_HELP},
                        tools=["query_kpi"], output_schema=output_schema("fix", emp.report),
                        max_turns=emp.limits["max_model_turns_per_step"], max_seconds=emp.limits["max_step_seconds"])
                except (StepLimitExceeded, LLMUnavailable, ToolFailure, MissingInput) as e:
                    self._pause(tid, "step_failed", f"自动修正第 {n} 轮失败：{e}。可人工修改后重新质检，或重试。")
                    return
                actions.append("模型按质检意见改写：" + "、".join(f["name"] for f in remaining))
                if old_resp and not content.get("feedback_response"):  # 修正时没带回员工对意见的说明：沿用
                    content["feedback_response"] = old_resp
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
        if task["artifact"].get("manual"):
            art["manual"] = True
        self.store.update(tid, artifact=art, qc_rounds=n)
        self.store.transition(tid, "fix_done", SYSTEM, {"reason": f"第 {n} 轮修正完成"})

    # ================================================================ 人工操作
    def _parse_review(self, tid: str, task: dict, emp: Employee, comments: str, history: list[str],
                      content_rel: str | None = None) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """意见拆成待办，并用数据核对意见本身（不调用模型）。content_rel：审核人看到的那一版内容（默认当前产物）。"""
        d = self.task_dir(tid)
        kpis = read_json(d / "work" / "kpis.json")
        mp = d / "work" / "minutes.json"
        minutes = read_json(mp) if mp.exists() else []
        cfg = emp.raw.get("feedback", {})
        todos = parse_feedback(comments, cfg, kpis, minutes if isinstance(minutes, list) else minutes.get("paragraphs", []),
                               history)
        rel = content_rel or (task.get("artifact") or {}).get("content_path")
        content = read_json(d / rel) if rel else None
        issues = check_feedback(todos, kpis, emp.quality_checks, content, cfg.get("cause_words"))
        return todos, issues

    def feedback_stuck(self, task: dict) -> bool:
        """因审核意见无法落实而暂停（自动修正用尽或员工认为不适用）：审核人可更正或撤回意见。"""
        if task["state"] != S.SUSPENDED or not task.get("artifact") or not self._feedback(task["id"], task["version"]):
            return False
        qc = self.task_dir(task["id"]) / "qc" / "qc_report.json"
        if not qc.exists():
            return False
        last = read_json(qc)["rounds"][-1]
        return last["version"] == task["version"] and any(
            r["rule"] == "feedback_addressed" and not r["passed"] for r in last["results"])

    def revise_feedback(self, tid: str, comments: str, actor: str = REVIEWER, run: bool = True,
                        user: str | None = None, confirm: bool = False) -> S:
        """审核人更正审核意见：按新意见重新返工（生成新版本，不计入返工次数，原意见留痕）。
        comments 为空表示撤回意见：当前版本不再校验该意见，直接重新质检。"""
        task = self.store.get(tid)
        emp = self._emp(task)
        if not self.feedback_stuck(task):
            raise IllegalTransition("只有因审核意见无法落实而暂停的任务才能更正或撤回意见")
        old = self._feedback(tid, task["version"])
        if not comments.strip():
            write_json(self.task_dir(tid) / "steps" / f"feedback@v{task['version']}.json",
                       {**old, "todos": [], "withdrawn": {"comments": old["comments"], "by": user}})
            self.store.transition(tid, "withdraw_feedback", actor, {"user": user, "feedback_stuck": True,
                                                                    "reason": f"撤回审核意见：{old['comments']}"},
                                  qc_rounds=0, resume_to=None)
            return self.drive(tid) if run else S.QC
        history = [fb["comments"] for v in range(2, task["version"]) if (fb := self._feedback(tid, v))]
        todos, issues = self._parse_review(tid, task, emp, comments, history, old["prev_content_path"])
        if issues and not confirm:
            self.store.event(tid, "feedback_checked", None, {"comments": comments, "issues": issues, "user": user})
            raise FeedbackCheck(issues)
        new_v = task["version"] + 1
        write_json(self.task_dir(tid) / "steps" / f"feedback@v{new_v}.json",
                   {"comments": comments, "todos": todos, "prev_content_path": old["prev_content_path"],
                    "revised_from": old["comments"]})
        self.store.transition(tid, "revise_feedback", actor, {"comments": comments, "user": user, "feedback_stuck": True,
                                                              "reason": f"更正审核意见：{old['comments']} → {comments}"},
                              version=new_v, qc_rounds=0, resume_to=None)
        self.store.event(tid, "feedback_parsed", None, {"version": new_v, "todos": todos, "revised_from": old["comments"]})
        return self.drive(tid) if run else S.REWORK

    def review(self, tid: str, decision: str, comments: str = "", reviewed_sha256: str | None = None,
               actor: str = REVIEWER, run: bool = True, user: str | None = None, confirm: bool = False) -> S:
        """confirm：退回前检查发现意见可能有误时（点名的对象不在异常清单、认不出的名称、要求写配置禁止的内容等），
        默认不流转、把问题交审核人确认；确认后仍可退回，问题随待办保存，员工可据此说明“不适用”。"""
        task = self.store.get(tid)
        emp = self._emp(task)
        event = "approve" if decision == "approve" else "request_changes"
        if (why := self_review_problem(task, {"user": user})):  # 先拦下，避免写入审核意见等副作用；闸门里还会再判一次
            self.store.reject(tid, event, actor, f"闸门未通过：{why}", task["state"])
        if (decision != "approve" and not confirm and actor == REVIEWER and task["state"] == S.PENDING_REVIEW
                and comments.strip() and task["rework_rounds"] < emp.limits["rework_max_rounds"]):
            history = [fb["comments"] for v in range(2, task["version"] + 1) if (fb := self._feedback(tid, v))]
            _, issues = self._parse_review(tid, task, emp, comments, history)
            if issues:  # 意见本身可能有误：不流转、不消耗返工次数，交审核人确认
                self.store.event(tid, "feedback_checked", None, {"comments": comments, "issues": issues, "user": user})
                raise FeedbackCheck(issues)
        self.store.event(tid, "review_submitted", None, {"decision": decision, "comments": comments, "actor": actor,
                                                         "user": user, "version": task["version"],
                                                         "reviewed_sha256": reviewed_sha256})
        if decision == "approve":
            self.store.transition(tid, "approve", actor, {"reviewed_sha256": reviewed_sha256, "reason": "审核通过",
                                                          "user": user})
            self._deliver(tid)
            return S.DELIVERED
        max_rw = emp.limits["rework_max_rounds"]
        if task["rework_rounds"] >= max_rw:
            return self.store.transition(tid, "request_changes_exhausted", actor,
                                         {"comments": comments, "max_rework_rounds": max_rw, "user": user},
                                         pause_reason=f"已返工 {max_rw} 次仍未通过，请审核人与发起人线下沟通后处理",
                                         resume_to=S.PENDING_REVIEW.value)
        history = [fb["comments"] for v in range(2, task["version"] + 1) if (fb := self._feedback(tid, v))]
        todos, _ = self._parse_review(tid, task, emp, comments, history)
        new_v = task["version"] + 1
        # 先落盘审核意见再流转：若在两步之间中断，返工时也一定读得到意见，不会原样复用上一版
        write_json(self.task_dir(tid) / "steps" / f"feedback@v{new_v}.json",
                   {"comments": comments, "todos": todos, "prev_content_path": task["artifact"]["content_path"]})
        self.store.transition(tid, "request_changes", actor, {"comments": comments, "max_rework_rounds": max_rw,
                                                              "reason": comments, "user": user},
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
                   actor: str = INITIATOR, run: bool = True, user: str | None = None) -> S:
        """补充材料或字段；fields 含 waive_<key> 时表示发起人确认没有该材料，记录确认人。"""
        task = self.store.get(tid)
        emp = self._emp(task)
        allowed = ({f["key"] for f in emp.form if f["type"] != "file"}
                   | {f"waive_{f['key']}" for f in emp.form if f.get("waivable")})
        unknown = [k for k in fields or {} if k not in allowed]
        if unknown:
            raise ValueError(f"不能补充的字段：{'、'.join(unknown)}（只能修改表单字段，或确认没有可跳过的材料）")
        form, names = dict(task["form"]), dict(task["meta"].get("file_names", {}))
        self._put_files(tid, emp, form, names, files or {}, append=True)
        fields = self._normalize(emp, {k: v for k, v in (fields or {}).items() if v not in (None, "")}) or None
        changed = {k: {"from": task["form"].get(k), "to": v} for k, v in (fields or {}).items()
                   if not k.startswith("waive_") and task["form"].get(k) != v}
        form.update(fields or {})
        meta = {**task["meta"], "file_names": names}
        if any(k.startswith("waive_") for k in fields or {}):
            meta["waived_by"] = user or task["meta"].get("created_by")
        self.store.transition(tid, "supplement", actor, {"files": files, "fields": fields, "user": user,
                                                         "reason": f"补充：{', '.join([*(files or {}), *(fields or {})])}"},
                              form=form, meta=meta)
        if changed:  # 改了统计周期等字段：记录改动；之前的步骤结果因输入变化不会被复用
            self.store.event(tid, "form_changed", None, {"fields": changed, "by": user or task["meta"].get("created_by")})
        return self.drive(tid) if run else S.ADMISSION

    def retry(self, tid: str, actor: str = INITIATOR, clear_faults: bool = False, run: bool = True,
              refresh_config: bool = False) -> S:
        """从断点重试。refresh_config：改用当前最新的员工配置（如管理员放宽了某条质检规则），默认沿用发起时的配置。"""
        task = self.store.get(tid)
        updates: dict[str, Any] = {"resume_to": None}
        if refresh_config:
            old = task["meta"].get("config_sha")
            sha = config_admin.snapshot(self.store, task["employee"], self.task_dir(tid))
            task["meta"] = {**task["meta"], "config_sha": sha}
            updates["meta"] = task["meta"]
            self.store.event(tid, "config_snapshot", None, {"employee": task["employee"], "config_sha": sha,
                                                            "previous": old, "reason": "重试时改用最新配置"})
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
        emp = self._emp(task)
        updates: dict[str, Any] = {"qc_rounds": 0, "resume_to": None}
        if content is not None:
            jsonschema.validate(content, output_schema("fix", emp.report))
            n = len(list((self.task_dir(tid) / "work").glob(f"content_v{task['version']}m*.json"))) + 1
            tag = f"v{task['version']}m{n}"
            res = self._render(task, emp, self._gateway(task, emp), content, task["version"], tag, "manual")
            art = self._artifact(task, emp, res, task["version"], tag=tag)
            art["fix_actions"] = ["人工修改内容"]
            art["manual"] = True  # 质检不通过时只做程序修正，不让模型改写人工修改的内容
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
