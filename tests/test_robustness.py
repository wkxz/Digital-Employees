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
