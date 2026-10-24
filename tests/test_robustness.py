"""复查中发现的问题的回归测试：跨平台锁、错数字不被“洗白”、修正缓存、上周不完整、同名重传、中断窗口。"""
import json
import shutil
from pathlib import Path

import pytest

from mini_employee.states import S

ROOT_DATA = Path(__file__).resolve().parents[1] / "data"


# ---------------------------------------------------------------- 1. 任务锁在 Windows / Linux 都可用

def test_exclusive_lock_is_portable_and_exclusive(tmp_path):
    from mini_employee.fsutil import exclusive_lock
    with exclusive_lock(tmp_path / ".lock") as first:
        assert first
        with exclusive_lock(tmp_path / ".lock") as second:
            assert not second
    with exclusive_lock(tmp_path / ".lock") as again:
        assert again


# ---------------------------------------------------------------- 2. 程序修正只认该句所述对象的数字

def _consistency(kpis, text):
    from mini_employee.qc.engine import program_fix
    from mini_employee.qc.rules import data_consistency
    from mini_employee.render import anomaly_flat
    from tests.test_core import _ctx
    content = {"summary": [text]}
    ok, details, _ = data_consistency(_ctx(kpis, content), {})
    assert not ok
    return program_fix(content, [{"rule": "data_consistency", "name": "数据一致性", "details": details}],
                       anomaly_flat(kpis), [])


def test_wrong_number_is_not_replaced_by_coincidentally_equal_kpi(kpis):
    from mini_employee.placeholders import fmt_value
    f = kpis["flat"]
    wrong = fmt_value("total.sales_wow", f["total.sales_wow"], absolute=True)  # 整体的数字，被写到华东头上
    assert wrong != fmt_value("region.华东.sales_wow", f["region.华东.sales_wow"], absolute=True)
    fixed, actions, remaining = _consistency(kpis, f"华东区销售额环比下降 {wrong}，拖累整体表现。")
    assert not actions and remaining, "写错的数字应交给模型改，而不是被换成数值碰巧相同的其他指标"
    assert wrong in fixed["summary"][0]


def test_correct_number_is_replaced_by_the_matching_entity_key(kpis):
    from mini_employee.placeholders import fmt_value
    v = fmt_value("total.sales_wow", kpis["flat"]["total.sales_wow"], absolute=True)
    fixed, actions, remaining = _consistency(kpis, f"本周整体销售额环比下降 {v}。")
    assert not remaining and "{{total.sales_wow|abs}}" in fixed["summary"][0]


def test_replacement_only_touches_the_flagged_clause(kpis):
    from mini_employee.qc.engine import program_fix
    from mini_employee.render import anomaly_flat
    from mini_employee.placeholders import fmt_value
    v = fmt_value("total.sales_wow", kpis["flat"]["total.sales_wow"], absolute=True)
    content = {"summary": ["本周整体环比下降 " + v + "。"],
               "anomalies": [{"id": "A1", "cause": f"纪要称客流下降 {v}", "evidence": [1]}]}
    d = {"type": "hardcoded", "section": "summary", "index": 0, "field": None, "text": v}
    fixed, _, _ = program_fix(content, [{"rule": "data_consistency", "details": [d]}], anomaly_flat(kpis), [])
    assert "{{total.sales_wow|abs}}" in fixed["summary"][0]
    assert fixed["anomalies"][0]["cause"] == f"纪要称客流下降 {v}"  # 原样引用纪要的数字不被改动


# ---------------------------------------------------------------- 3. 修正超限后重试 / 人工修改能真正生效

def _exhausted(orch, sample_files):
    tid = orch.create("weekly_report", {"period": "2026-W44"}, sample_files,
                      {"faults": [{"tool": "mock", "mode": "direction_error"}, {"tool": "mock", "mode": "stubborn"}]})
    assert orch.drive(tid) == S.SUSPENDED
    return tid


def _fix_runs(orch, tid):
    return sum(1 for e in orch.store.events(tid, ["step_started"]) if e["step_id"] == "fix")


def test_retry_after_fix_exhausted_runs_fixes_again(orch, sample_files):
    tid = _exhausted(orch, sample_files)
    before = _fix_runs(orch, tid)
    orch.retry(tid)
    assert _fix_runs(orch, tid) == before + 2, "重试应重新修正，而不是复用上一轮的缓存结果"
    tags = [e["payload"]["key"] for e in orch.store.events(tid, ["step_completed"])
            if e["step_id"] == "fix"]
    assert len(tags) == len(set(tags))


def test_manual_edit_is_kept_after_recheck(orch, sample_files):
    from mini_employee.fsutil import read_json
    tid = _exhausted(orch, sample_files)
    t = orch.store.get(tid)
    content = read_json(orch.task_dir(tid) / t["artifact"]["content_path"])
    content["summary"][0] = "人工修改标记：" + content["summary"][0]
    orch.manual_recheck(tid, content=content)
    t = orch.store.get(tid)
    final = read_json(orch.task_dir(tid) / t["artifact"]["content_path"])
    assert "人工修改标记" in json.dumps(final, ensure_ascii=False), "人工修改不应被旧的修正结果覆盖"


# ---------------------------------------------------------------- 4. 上周数据不完整要暂停

def test_incomplete_previous_week_is_missing_input():
    from datetime import timedelta

    import pandas as pd

    from mini_employee.periods import parse_week
    from mini_employee.tools import data as D
    df, _ = D.load_sales(ROOT_DATA / "samples" / "销售明细_2026W44.xlsx", {},
                         ["日期", "门店", "区域", "品类", "销售额", "销量"])
    df, _ = D.clean_sales(df)
    pw = parse_week("2026-W44").prev
    gap = pd.Timestamp(pw.start + timedelta(days=2))
    with pytest.raises(D.DataError) as e:
        D.compute_kpis(df[df["日期"] != gap], parse_week("2026-W44"), 0.15, 5, 20000, ["region"])
    assert e.value.kind == "MISSING_INPUT" and "上一周" in e.value.message and gap.date().isoformat() in e.value.message


# ---------------------------------------------------------------- 5. 同名更正版重传

def test_same_name_corrected_file_is_kept(tmp_path):
    from mini_employee.config import load_employee
    from mini_employee.tools import data as D
    adm = load_employee("weekly_report").admission
    cur = ROOT_DATA / "fixtures" / "销售明细_仅本周.xlsx"
    prev = next((ROOT_DATA / "user_cases" / "case_02_missing_last_week").glob("补充_*.xlsx"))
    names = []
    for i, (src, name) in enumerate([(cur, "销售明细.xlsx"), (prev, "上周.xlsx"), (cur, "销售明细.xlsx")]):
        shutil.copy(src, tmp_path / f"{i}_{name}")
        names.append(tmp_path / f"{i}_{name}")
    _, info = D.load_sales_many(names, adm["column_aliases"], adm["required_columns"])
    assert info["superseded_index"] == [0]
    assert {"2026-W43", "2026-W44"} <= set(info["weeks"])


# ---------------------------------------------------------------- 6. 中断窗口

def test_feedback_is_saved_before_entering_rework(orch, sample_files, monkeypatch):
    tid = orch.create("weekly_report", {"period": "2026-W44"}, sample_files)
    orch.drive(tid)
    seen = {}
    real = orch.store.transition

    def spy(tid_, event, *a, **kw):
        if event == "request_changes":
            seen["exists"] = (orch.task_dir(tid_) / "steps" / "feedback@v2.json").exists()
        return real(tid_, event, *a, **kw)

    monkeypatch.setattr(orch.store, "transition", spy)
    orch.review(tid, "changes", "华东区下滑原因没说清楚，请结合周会纪要补充", run=False)
    assert seen["exists"], "进入返工前审核意见必须已落盘"


def test_incomplete_deliverable_is_rebuilt_on_resume(orch, sample_files):
    tid = orch.create("weekly_report", {"period": "2026-W44"}, sample_files)
    orch.drive(tid)
    orch.review(tid, "approve", reviewed_sha256=orch.store.get(tid)["artifact"]["sha256"])
    manifest = orch.task_dir(tid) / "deliverable" / "manifest.json"
    manifest.unlink()  # 模拟审核通过后、交付包写完前进程被中断
    orch.resume_all()
    assert manifest.exists()


# ---------------------------------------------------------------- 7. 送审 / 交付闸门读质检记录本身

def test_qc_pass_gate_reads_the_qc_record_not_the_flag(orch, sample_files):
    from mini_employee.store import IllegalTransition
    tid = orch.create("weekly_report", {"period": "2026-W44"}, sample_files)
    orch.drive(tid)
    t = orch.store.get(tid)
    orch.store.update(tid, state=S.QC, artifact={**t["artifact"], "sha256": "被替换过的产物"})
    with pytest.raises(IllegalTransition, match="最近一次质检的不是当前产物"):
        orch.store.transition(tid, "qc_pass", "system", {"qc_passed": True})  # 传入的标志不再被信任


def test_approve_gate_rejects_when_last_qc_round_failed(orch, sample_files):
    from mini_employee.fsutil import read_json, write_json
    from mini_employee.store import IllegalTransition
    tid = orch.create("weekly_report", {"period": "2026-W44"}, sample_files)
    orch.drive(tid)
    path = orch.task_dir(tid) / "qc" / "qc_report.json"
    data = read_json(path)
    data["rounds"][-1]["passed"] = False
    write_json(path, data)
    with pytest.raises(IllegalTransition, match="质检未通过"):
        orch.review(tid, "approve", reviewed_sha256=orch.store.get(tid)["artifact"]["sha256"])
    assert orch.store.get(tid)["state"] == S.PENDING_REVIEW


# ---------------------------------------------------------------- 8. 状态与事件同一事务

def test_state_and_event_are_written_atomically(orch, sample_files, monkeypatch):
    tid = orch.create("weekly_report", {"period": "2026-W44"}, sample_files)
    real = orch.store.event

    def boom(tid_, type_, *a, **kw):
        if type_ == "state_changed":
            raise RuntimeError("模拟写事件时进程出错")
        return real(tid_, type_, *a, **kw)

    monkeypatch.setattr(orch.store, "event", boom)
    with pytest.raises(RuntimeError):
        orch.store.transition(tid, "submit", "system", {})
    monkeypatch.undo()
    assert orch.store.get(tid)["state"] == S.CREATED, "事件没写成功，状态也不应改变"
    assert orch.store.transition(tid, "submit", "system", {}) == S.ADMISSION  # 事务已回滚，连接可继续使用


# ---------------------------------------------------------------- 9. 工具内部错误不重试

def test_internal_tool_error_is_not_retried(tmp_path):
    from mini_employee.tools.errors import INTERNAL, ToolFailure
    from tests.test_core import _gateway
    ev: list = []
    gw = _gateway(tmp_path, ev, max_retries=3)
    args = {"content_path": "nope.json", "version_tag": "x", "formats": ["docx"], "report_config": {}, "meta": {},
            "idempotency_key": "k"}
    with pytest.raises(ToolFailure) as e:
        gw.call("generate_report", args, allowed=["generate_report"], step_id="render")
    assert e.value.kind == INTERNAL and e.value.attempts == 1
    assert [t for t, _ in ev].count("tool_failed") == 1


def test_filename_may_reference_an_unfilled_optional_field(tmp_path, monkeypatch, orch, sample_files):
    import yaml
    import mini_employee.config as C
    base = tmp_path / "employees"
    shutil.copytree(Path(__file__).resolve().parents[1] / "employees", base)
    monkeypatch.setattr(C, "EMPLOYEES_DIR", base)
    path = base / "weekly_report" / "employee.yaml"
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    raw["report"]["filename"] = "经营周报_{period_compact}{focus_region}"
    path.write_text(yaml.safe_dump(raw, allow_unicode=True, sort_keys=False), encoding="utf-8")
    tid = orch.create("weekly_report", {"period": "2026-W44"}, sample_files)  # 未填 focus_region
    assert orch.drive(tid) == S.PENDING_REVIEW
    assert orch.store.get(tid)["artifact"]["primary"].endswith("经营周报_2026W44.docx")


# ---------------------------------------------------------------- 10. pii_policy=redact 真正脱敏

def _redact_employee(tmp_path, monkeypatch):
    import yaml
    import mini_employee.config as C
    base = tmp_path / "employees"
    shutil.copytree(Path(__file__).resolve().parents[1] / "employees", base)
    monkeypatch.setattr(C, "EMPLOYEES_DIR", base)
    path = base / "weekly_report" / "employee.yaml"
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    raw["admission"]["pii_policy"] = "redact"
    path.write_text(yaml.safe_dump(raw, allow_unicode=True, sort_keys=False), encoding="utf-8")


def test_redact_policy_drops_pii_columns_and_masks_minutes(tmp_path, monkeypatch, orch):
    from mini_employee.fsutil import read_json
    from mini_employee.runner import _report_text
    _redact_employee(tmp_path, monkeypatch)
    minutes = tmp_path / "周会纪要.md"
    minutes.write_text("# 周会纪要\n\n华东区店长电话 13812345678，客流下降。\n\n华南区生鲜表现良好。\n", encoding="utf-8")
    d = ROOT_DATA / "user_cases" / "case_07_privacy_leak"
    tid = orch.create("weekly_report", {"period": "2026-W44"},
                      {"sales_file": str(d / "销售明细_含隐私信息.xlsx"), "minutes_file": str(minutes)})
    assert orch.drive(tid) == S.PENDING_REVIEW
    clean = (orch.task_dir(tid) / "work" / "sales_clean.csv").read_text(encoding="utf-8")
    assert "会员手机号" not in clean.splitlines()[0] and "13812345678" not in clean, "隐私列不应落盘"
    saved = json.dumps(read_json(orch.task_dir(tid) / "work" / "minutes.json"), ensure_ascii=False)
    assert "13812345678" not in saved and "138****5678" in saved, "纪要中的手机号应打码后再交给模型"
    prompts = json.dumps([e["payload"] for e in orch.store.events(tid, ["tool_called"])], ensure_ascii=False)
    assert "13812345678" not in prompts
    text = _report_text(orch, tid)
    assert "隐私保护：销售明细中的“会员手机号”" in text and "周会纪要中 1 处" in text
    assert "13812345678" not in text


# ---------------------------------------------------------------- 11. 纪要说法核对：识别口语说法，不误判

def test_claims_recognize_colloquial_sales_claims_and_skip_negation():
    from mini_employee.reconcile import claims
    got = claims("华北区总监声称：“受修路影响，华北区本周销售额暴跌50%，客流大幅腰斩。”")
    assert [(c["text"], c["value"]) for c in got] == [("暴跌50%", -50.0)]
    assert claims("实际仅微跌-3.11%，并非暴跌50%") == []          # 没有“销售”，且“并非”是否定
    assert claims("客流下降30%，客单价提升5%") == []               # 其他口径不比较
    within = claims("各区域销售波动均在±2%以内的常规合理区间")
    assert within and within[0]["kind"] == "within" and within[0]["value"] == 2.0
    assert claims("各项关键指标（毛利、周转天数）波动幅度在 ±3% 正常合理区间内") == []


def test_extra_notes_attribute_claims_to_the_right_object(kpis):
    from mini_employee.reconcile import extra_notes
    r = next(x for x in kpis["tables"]["region"])
    wow = kpis["flat"][f"region.{r['name']}.sales_wow"] * 100
    far = -50.0 if wow > -40 else 50.0
    minutes = [{"no": 1, "ref": "第1条", "header": None, "tag": None,
                "text": f"{r['name']}区本周销售额{'暴跌' if far < 0 else '暴涨'}{abs(far):.0f}%。"},
               {"no": 2, "ref": "第2条", "header": None, "tag": None,
                "text": f"{r['name']}区本周销售额环比{'下降' if wow < 0 else '增长'}{abs(wow):.1f}%。"}]
    notes = extra_notes({"anomalies": []}, kpis, minutes)
    assert len(notes) == 1 and notes[0]["minutes_no"] == 1, "只有不一致的说法才写核对说明"
    assert f"{r['name']}区销售额环比为 {wow:+.2f}%" in notes[0]["text"]
    # 说不清是哪个对象的说法不核对，避免张冠李戴
    assert extra_notes({"anomalies": []}, kpis, [{**minutes[0], "text": "部分门店销售额暴跌50%。"}]) == []
