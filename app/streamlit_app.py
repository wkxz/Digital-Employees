"""迷你数字员工 · Web 界面（Streamlit）

页面：发起任务 / 任务中心 / 审核台 / 事件回放 / 故障注入 / 员工配置
启动：make ui  或  uv run streamlit run app/streamlit_app.py
"""
from __future__ import annotations

import difflib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

import pandas as pd
import streamlit as st
import yaml

from mini_employee.config import EMPLOYEES_DIR, ROOT, load_employee, parse_skill_md, read_employee_files, runs_dir
from mini_employee.fsutil import read_json
from mini_employee.orchestrator import Orchestrator
from mini_employee.render import build_blocks, write_md
from mini_employee.states import INITIATOR, LABELS, REVIEWER, TERMINAL, S, table_markdown
from mini_employee.store import IllegalTransition
from mini_employee.validate import ConfigError, validate

st.set_page_config(page_title="迷你数字员工", page_icon="📊", layout="wide")
EMP_ID = "weekly_report"
STATE_COLOR = {S.DELIVERED: "green", S.REJECTED: "gray", S.CANCELLED: "gray", S.SUSPENDED: "orange",
               S.WAITING_INPUT: "orange", S.PENDING_REVIEW: "blue"}


@st.cache_resource
def orch() -> Orchestrator:
    return Orchestrator()


def badge(state: S) -> str:
    return f":{STATE_COLOR.get(state, 'violet')}[**{LABELS[state]}**]"


def run_bg(tid: str) -> None:
    """后台执行，界面不阻塞（真实模型一次任务需 1–3 分钟）。"""
    subprocess.Popen([sys.executable, "-m", "mini_employee.cli", "run", tid], cwd=ROOT, env=os.environ.copy(),
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def save_upload(f) -> Path:
    d = Path(tempfile.mkdtemp(dir=runs_dir()))
    p = d / f.name
    p.write_bytes(f.getbuffer())
    return p


def act(fn, *args, **kw) -> Any:
    """执行一个操作；失败时在页面上说明原因，而不是显示报错堆栈。成功返回 fn 的结果（至少为 True）。"""
    try:
        return fn(*args, **kw) or True
    except IllegalTransition as e:
        st.error(f"操作被拒绝：{e}")
    except ConfigError as e:
        st.error("员工配置有误，请到“员工配置”页改正：\n\n" + "\n".join(f"- {x}" for x in e.errors))
    except Exception as e:  # noqa: BLE001  如并发修改冲突、人工修改的内容不符合格式
        st.error(f"操作失败：{type(e).__name__}: {e}")
    return False


# ---------------------------------------------------------------- sidebar
st.sidebar.title("📊 迷你数字员工")
st.sidebar.caption("经营周报员工 · 可审核、可交付")
page = st.sidebar.radio("页面", ["发起任务", "任务中心", "审核台", "事件回放", "故障注入", "员工配置"])
role = st.sidebar.selectbox("当前身份", [INITIATOR, REVIEWER], format_func=lambda r: {"initiator": "发起人",
                                                                                   "reviewer": "审核人"}[r])
try:
    model = os.environ.get("MINI_MODEL") or (read_employee_files(EMP_ID)[1].get("model") or {}).get("provider", "mock")
except Exception:  # noqa: BLE001  YAML 写坏时下面会提示，这里不中断页面
    model = "?"
st.sidebar.caption(f"模型：{model}　数据目录：{runs_dir()}")
if st.sidebar.button("🔄 刷新"):
    st.rerun()

o = orch()
try:
    emp = load_employee(EMP_ID)
except Exception as e:  # noqa: BLE001  配置写错时其他页面无法工作，只开放“员工配置”页供改正
    errs = e.errors if isinstance(e, ConfigError) else [str(e)]
    st.error("员工配置有误，请在“员工配置”页改正后刷新：\n\n" + "\n".join(f"- {x}" for x in errs))
    emp, page = None, "员工配置"


def task_picker(states: set[S] | None = None, key: str = "pick") -> dict | None:
    tasks = [t for t in o.store.list() if not states or t["state"] in states]
    if not tasks:
        st.info("暂无任务")
        return None
    labels = {t["id"]: f"{t['id']} · {t['form'].get('period', '')} · {LABELS[t['state']]} · V{t['version']}" for t in tasks}
    tid = st.selectbox("选择任务", list(labels), format_func=labels.get, key=key)
    return o.store.get(tid)


def report_preview(t: dict) -> None:
    d = o.task_dir(t["id"])
    art = t["artifact"]
    content = read_json(d / art["content_path"])
    kpis = read_json(d / "work" / "kpis.json")
    charts = {p.stem: str(p) for p in (d / "work" / "charts").glob("*.png")}
    blocks, _, _ = build_blocks(content, kpis, charts, emp.report, {**t["form"], "version": t["version"],
                                                                     "period_label": kpis["period_label"],
                                                                     "generated_at": "", "footer": emp.report.get("footer", ""),
                                                                     "sources": [n for v in t["meta"].get("file_names", {}).values()
                                                                                 for n in (v if isinstance(v, list) else [v])]},
                             read_json(d / "work" / "minutes.json"))
    for kind, val in blocks:
        if kind == "title":
            st.subheader(val)
        elif kind == "meta":
            st.caption(val)
        elif kind == "h2":
            st.markdown(f"#### {val}")
        elif kind == "p":
            st.write(val)
        elif kind == "bullet":
            st.markdown(f"- {val}")
        elif kind == "anomaly":
            st.markdown(f"**{val['head']}**")
            for lab, k in (("现象", "phenomenon"), ("原因", "cause"), ("建议", "suggestion")):
                if val[k]:
                    st.markdown(f"&nbsp;&nbsp;**{lab}**：{val[k]}")
        elif kind == "table":
            st.caption(val["title"])
            st.dataframe(pd.DataFrame(val["rows"], columns=val["columns"]), hide_index=True, width="stretch")
        elif kind == "image":
            st.image(val["path"], width=640)


def content_markdown(d: Path, rel: str, t: dict) -> list[str]:
    content = read_json(d / rel)
    kpis = read_json(d / "work" / "kpis.json")
    blocks, _, _ = build_blocks(content, kpis, {}, emp.report, {**t["form"], "version": 0, "period_label": "",
                                                                 "generated_at": ""})
    tmp = d / ".preview.md"
    write_md(blocks, tmp)
    return tmp.read_text(encoding="utf-8").splitlines()


# ================================================================ 发起任务
if page == "发起任务":
    st.header("发起任务")
    st.caption(emp.raw.get("description", ""))
    with st.form("new"):
        values, files = {}, {}
        for fld in emp.form:
            label = fld["label"] + (" *" if fld.get("required") else "")
            if fld["type"] == "file":
                files[fld["key"]] = st.file_uploader(label, type=fld.get("ext"), key=fld["key"],
                                                     accept_multiple_files=bool(fld.get("multiple")))
            elif fld["type"] == "enum":
                values[fld["key"]] = st.selectbox(label, [""] + fld["options"])
            else:
                values[fld["key"]] = st.text_input(label, help=fld.get("help"), placeholder=fld.get("help", ""))
        use_sample = st.checkbox("未上传的文件使用内置样例数据（第 44 周）", value=False)
        with st.expander("故障注入（演示用）"):
            fs = st.multiselect("本任务注入的故障", ["search_minutes:timeout", "read_minutes:timeout", "llm:timeout",
                                                 "mock:direction_error", "mock:hardcode_number", "mock:forbidden_tool",
                                                 "mock:loop_tools"])
        submitted = st.form_submit_button("提交任务", type="primary")
    if submitted:
        sample = {"sales_file": ROOT / "data/samples/销售明细_2026W44.xlsx",
                  "minutes_file": ROOT / "data/samples/周会纪要_2026W44.docx"}
        paths = {}
        for k, f in files.items():
            if isinstance(f, list) and f:
                paths[k] = [save_upload(x) for x in f]
            elif f and not isinstance(f, list):
                paths[k] = save_upload(f)
            elif use_sample and k in sample:
                paths[k] = sample[k]
        faults = [{"tool": x.split(":")[0], "mode": x.split(":")[1]} for x in fs]
        tid = act(o.create, EMP_ID, values, paths, {"faults": faults})
        if tid:
            run_bg(tid)
            st.success(f"已创建任务 {tid}，正在后台执行。到“任务中心”查看进度。")

# ================================================================ 任务中心
elif page == "任务中心":
    st.header("任务中心")
    tasks = o.store.list()
    if tasks:
        st.dataframe(pd.DataFrame([{"任务": t["id"], "周期": t["form"].get("period", ""), "状态": LABELS[t["state"]],
                                    "版本": f"V{t['version']}", "说明": t["pause_reason"] or "", "更新": t["updated_at"][5:19]}
                                   for t in tasks]), hide_index=True, width="stretch")
    t = task_picker()
    if t:
        tid = t["id"]
        st.markdown(f"### {tid}　{badge(t['state'])}")
        if t["pause_reason"]:
            (st.warning if t["state"] not in TERMINAL else st.error)(t["pause_reason"])
        steps = o.store.events(tid, ["step_started", "step_completed"])
        done = {e["payload"]["key"] for e in steps if e["type"] == "step_completed"}
        cols = st.columns(len(emp.steps))
        for c, s in zip(cols, emp.steps):
            key = f"{s['id']}@v{t['version']}" if s.get("per_version") else s["id"]
            c.metric(s.get("name", s["id"]), "✓" if key in done else "…")
        if t["state"] == S.WAITING_INPUT:
            st.subheader("补充材料")
            st.caption("销售明细可只上传缺少的部分（如上周数据），会与已上传的数据自动合并。")
            ups = {f["key"]: st.file_uploader(f["label"], type=f.get("ext"), key=f"sup_{f['key']}",
                                              accept_multiple_files=bool(f.get("multiple")))
                   for f in emp.form if f["type"] == "file"}
            if st.button("提交补充材料", type="primary"):
                paths = {k: ([save_upload(x) for x in f] if isinstance(f, list) else save_upload(f))
                         for k, f in ups.items() if f}
                if act(o.supplement, tid, paths, None, actor=role, run=False):
                    run_bg(tid)
                    st.rerun()
        if t["state"] == S.SUSPENDED:
            c1, c2 = st.columns(2)
            clear = c1.checkbox("同时清除本任务的故障注入（模拟服务恢复）", value=True)
            if c1.button("从断点重试", type="primary") and act(o.retry, tid, role, clear_faults=clear, run=False):
                run_bg(tid)
                st.rerun()
            if t["artifact"] and c2.button("人工确认后重新质检") and act(o.manual_recheck, tid, None, role, run=False):
                run_bg(tid)
                st.rerun()
        if t["state"] not in TERMINAL and st.button("取消任务"):
            act(o.cancel, tid, role)
            st.rerun()
        if t["state"] == S.DELIVERED:
            dd = o.task_dir(tid) / "deliverable"
            st.success(f"已交付：{dd}")
            for p in sorted(dd.iterdir()):
                st.download_button(f"下载 {p.name}", p.read_bytes(), file_name=p.name, key=str(p))

# ================================================================ 审核台
elif page == "审核台":
    st.header("审核台")
    t = task_picker({S.PENDING_REVIEW, S.DELIVERED}, key="review_pick")
    if t and t["artifact"]:
        tid, d, art = t["id"], o.task_dir(t["id"]), t["artifact"]
        st.markdown(f"### {tid}　{badge(t['state'])}　版本 V{t['version']}（{art['tag']}）")
        tab1, tab2, tab3, tab4 = st.tabs(["周报预览", "质检记录", "与上一版的差异", "审核意见采纳情况"])
        with tab1:
            st.download_button("下载 Word", (d / art["primary"]).read_bytes(), file_name=Path(art["primary"]).name)
            report_preview(t)
        with tab2:
            qc = d / "qc" / "质检记录.md"
            st.markdown(qc.read_text(encoding="utf-8") if qc.exists() else "暂无")
        with tab3:
            prev = art.get("prev_content_path")
            if prev:
                diff = difflib.unified_diff(content_markdown(d, prev, t), content_markdown(d, art["content_path"], t),
                                            "上一版", "本版", lineterm="", n=0)
                st.code("\n".join(diff) or "无差异", language="diff")
            else:
                st.info("这是第一版")
        with tab4:
            fb = d / "steps" / f"feedback@v{t['version']}.json"
            if fb.exists():
                f = read_json(fb)
                st.write(f"审核意见：{f['comments']}")
                st.dataframe(pd.DataFrame(f["todos"]), hide_index=True)
            else:
                st.info("本版本没有待落实的审核意见")
        if t["state"] == S.PENDING_REVIEW:
            st.divider()
            comments = st.text_area("退回意见（退回时必填，可写多条，用分号或换行分隔）")
            c1, c2 = st.columns(2)
            if c1.button("✅ 审核通过并交付", type="primary"):
                if act(o.review, tid, "approve", reviewed_sha256=art["sha256"], actor=role):
                    st.success("已交付")
                    st.rerun()
            if c2.button("↩️ 退回修改"):
                if act(o.review, tid, "changes", comments, actor=role, run=False):
                    run_bg(tid)
                    st.success("已退回，员工正在按意见重做")
                    st.rerun()

# ================================================================ 事件回放
elif page == "事件回放":
    st.header("事件回放")
    t = task_picker(key="ev_pick")
    if t:
        evs = o.store.events(t["id"])
        types = sorted({e["type"] for e in evs})
        sel = st.multiselect("事件类型", types, default=types)
        rows = []
        for e in evs:
            if e["type"] not in sel:
                continue
            p = e["payload"]
            brief = {"state_changed": lambda: f"{p['from']} → {p['to']}（{p['event']}，{p['actor']}）{p.get('reason') or ''}",
                     "tool_called": lambda: f"{p['tool']} 第{p['attempt']}次 {p['ms']}ms",
                     "tool_failed": lambda: f"{p['tool']} 第{p.get('attempt')}次失败：{p['error']}",
                     "tool_denied": lambda: f"拒绝 {p['tool']}（开放：{p.get('allowed')}）",
                     "model_called": lambda: f"第{p['turn']}轮 {p['ms']}ms 工具调用：{[c['name'] for c in p['tool_calls']]}",
                     "qc_run": lambda: f"第{p['round_no']}次 {'通过' if p['passed'] else '未通过：' + ','.join(p['failed'])}",
                     "transition_rejected": lambda: p["reason"]}.get(e["type"], lambda: json.dumps(p, ensure_ascii=False)[:120])()
            rows.append({"序号": e["seq"], "时间": e["ts"][11:23], "事件": e["type"], "步骤": e["step_id"] or "", "摘要": brief})
        st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch", height=420)
        seq = st.number_input("查看事件详情（序号）", min_value=1, value=evs[-1]["seq"] if evs else 1)
        ev = next((e for e in evs if e["seq"] == seq), None)
        if ev:
            st.json(ev["payload"])

# ================================================================ 故障注入
elif page == "故障注入":
    st.header("故障注入（全局，现场演示用）")
    st.caption("对之后的所有工具调用生效；也可以在发起任务时只对单个任务注入。")
    path = runs_dir() / "faults.json"
    cur = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
    options = {"检索服务超时（search_minutes）": {"tool": "search_minutes", "mode": "timeout"},
               "纪要读取服务超时（read_minutes）": {"tool": "read_minutes", "mode": "timeout"},
               "KPI 查询服务 503（query_kpi）": {"tool": "query_kpi", "mode": "error"},
               "模型接口断开（llm）": {"tool": "llm", "mode": "timeout"}}
    chosen = [k for k, v in options.items() if st.toggle(k, value=v in cur)]
    if st.button("保存", type="primary"):
        path.write_text(json.dumps([options[k] for k in chosen], ensure_ascii=False), encoding="utf-8")
        st.success("已保存")

# ================================================================ 员工配置
elif page == "员工配置":
    st.header("员工配置")
    st.caption("业务人员改这里即可调整员工做法，无需改代码。保存前自动校验，有错误不会写入；保存后对新执行的步骤生效。")
    tab1, tab2, tab3 = st.tabs(["employee.yaml（结构）", "SKILL.md（做法）", "状态流转表"])
    d = EMPLOYEES_DIR / EMP_ID
    for tab, name in ((tab1, "employee.yaml"), (tab2, "SKILL.md")):
        with tab:
            p = d / name
            text = st.text_area(name, p.read_text(encoding="utf-8"), height=520, key=name)
            if st.button(f"保存 {name}", key=f"save_{name}"):
                try:  # 先校验改后的整套配置，通过了才写盘
                    is_yaml = name.endswith(".yaml")
                    raw = yaml.safe_load(text if is_yaml else (d / "employee.yaml").read_text(encoding="utf-8")) or {}
                    skill = (d / "SKILL.md").read_text(encoding="utf-8") if is_yaml else text
                    errors, warnings = validate(raw, parse_skill_md(skill)[1])
                except Exception as e:  # noqa: BLE001  YAML 语法错误
                    errors, warnings = [f"{type(e).__name__}: {e}"], []
                if errors:
                    st.error("配置有误，未保存：\n\n" + "\n".join(f"- {x}" for x in errors))
                else:
                    p.write_text(text, encoding="utf-8", newline="\n")
                    st.success("已保存，校验通过")
                for w in warnings:
                    st.warning(w)
    with tab3:
        st.markdown(table_markdown())
