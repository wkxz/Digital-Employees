"""审核人写错意见时：
A. 退回前用数据核对意见本身（不调用模型）：点名对象不在异常清单、认不出的名称、要求写配置禁止的内容 → 先提示审核人；
B. 审核人确认后仍退回：员工可说明“不适用”，程序以核实结果为准放行并告知审核人；无法核实的直接转审核人确认，不空耗修正轮次；
C. 因意见无法落实而暂停时：审核人可更正或撤回意见。"""
import pytest

from mini_employee.fsutil import read_json
from mini_employee.payloads import FeedbackCheck, check_feedback, parse_feedback
from mini_employee.states import INITIATOR, S
from mini_employee.store import IllegalTransition


@pytest.fixture
def pending(orch, sample_files):
    tid = orch.create("weekly_report", {"period": "2026-W44"}, sample_files, user="张三")
    assert orch.drive(tid) == S.PENDING_REVIEW
    return orch, tid


def _issues(orch, tid, comment):
    from mini_employee.config import load_employee
    emp = load_employee("weekly_report")
    d = orch.task_dir(tid)
    kpis, minutes = read_json(d / "work" / "kpis.json"), read_json(d / "work" / "minutes.json")
    todos = parse_feedback(comment, emp.raw["feedback"], kpis, minutes)
    return [i["kind"] for i in check_feedback(todos, kpis, emp.quality_checks,
                                              read_json(d / orch.store.get(tid)["artifact"]["content_path"]))]


def test_precheck_flags_wrong_comments_only(pending):
    orch, tid = pending
    assert _issues(orch, tid, "华北区下滑原因没说清楚，请结合周会纪要补充") == ["not_anomaly"]
    assert _issues(orch, tid, "西南门店的原因写得不对") == ["not_anomaly"]
    assert _issues(orch, tid, "华冬区下滑原因没说清楚") == ["unknown_name"]
    assert _issues(orch, tid, "请补充客单价分析") == ["forbidden"]
    for ok in ["华东区下滑原因没说清楚，请结合周会纪要补充", "摘要请补充目标达成率", "下周关注事项请写明负责人",
               "杭州湖滨店停业原因再写具体些", "静安店的原因写得不对", "品类部分写得太长；摘要改为3条"]:
        assert _issues(orch, tid, ok) == [], ok


def test_wrong_comment_is_held_before_rework(pending):
    orch, tid = pending
    with pytest.raises(FeedbackCheck, match="华北本周实收环比 .*不在异常清单中"):
        orch.review(tid, "changes", "华北区下滑原因没说清楚，请结合周会纪要补充", actor="reviewer", user="李四")
    t = orch.store.get(tid)
    assert t["state"] == S.PENDING_REVIEW and t["rework_rounds"] == 0 and t["version"] == 1  # 不流转、不消耗返工次数
    assert orch.store.events(tid, ["feedback_checked"]) and not orch.store.events(tid, ["review_submitted"])


def test_confirmed_wrong_comment_employee_explains_and_program_verifies(pending):
    orch, tid = pending
    c = "华北区下滑原因没说清楚，请结合周会纪要补充"
    assert orch.review(tid, "changes", c, actor="reviewer", user="李四", confirm=True) == S.PENDING_REVIEW
    rounds = read_json(orch.task_dir(tid) / "qc" / "qc_report.json")["rounds"]
    fa = next(r for r in rounds[-1]["results"] if r["rule"] == "feedback_addressed")
    assert fa["passed"] and "员工说明意见" in fa["details"][0]["msg"] and "程序核实：华北本周实收环比" in fa["details"][0]["msg"]
    assert not any(e["payload"]["key"].startswith("fix@v2") for e in orch.store.events(tid, ["step_completed"]))


def _dispute(monkeypatch):
    """让模型对一条正常的意见说“不适用”（程序无法核实）。"""
    import mini_employee.orchestrator as O
    real = O.run_agent_step

    def spy(*a, **kw):
        out = real(*a, **kw)
        if kw.get("purpose") == "analysis" and kw["payload"].get("feedback"):
            out["feedback_response"] = [{"instruction": f["instruction"], "status": "not_applicable",
                                         "reason": "我认为原因已经写清楚了"} for f in kw["payload"]["feedback"]]
        return out
    monkeypatch.setattr(O, "run_agent_step", spy)
    return lambda: monkeypatch.setattr(O, "run_agent_step", real)


def _stuck(pending, monkeypatch):
    orch, tid = pending
    undo = _dispute(monkeypatch)
    assert orch.review(tid, "changes", "华东区下滑原因没说清楚，请结合周会纪要补充", actor="reviewer", user="李四") == S.SUSPENDED
    undo()
    t = orch.store.get(tid)
    assert "程序无法用数据核实" in t["pause_reason"] and "更正审核意见" in t["pause_reason"]
    assert orch.feedback_stuck(t)
    assert not any(e["payload"]["key"].startswith("fix@v2") for e in orch.store.events(tid, ["step_completed"]))
    return orch, tid


def test_unverifiable_dispute_goes_to_reviewer_without_fix_rounds(pending, monkeypatch):
    _stuck(pending, monkeypatch)


def test_reviewer_revises_comment_after_stuck(pending, monkeypatch):
    orch, tid = _stuck(pending, monkeypatch)
    with pytest.raises(IllegalTransition):
        orch.revise_feedback(tid, "杭州湖滨店停业原因再写具体些", actor=INITIATOR, user="张三")
    with pytest.raises(IllegalTransition, match="不能审核自己"):
        orch.revise_feedback(tid, "杭州湖滨店停业原因再写具体些", user="张三")
    assert orch.revise_feedback(tid, "摘要改为3条；核心指标部分补充销量分析", user="李四") == S.PENDING_REVIEW
    t = orch.store.get(tid)
    assert t["version"] == 3 and t["rework_rounds"] == 1  # 更正意见不计返工次数
    fb = read_json(orch.task_dir(tid) / "steps" / "feedback@v3.json")
    assert fb["revised_from"] == "华东区下滑原因没说清楚，请结合周会纪要补充" and len(fb["todos"]) == 2


def test_reviewer_withdraws_comment_after_stuck(pending, monkeypatch):
    orch, tid = _stuck(pending, monkeypatch)
    assert orch.revise_feedback(tid, "", user="李四") == S.PENDING_REVIEW
    t = orch.store.get(tid)
    assert t["version"] == 2
    fb = read_json(orch.task_dir(tid) / "steps" / "feedback@v2.json")
    assert fb["todos"] == [] and fb["withdrawn"]["by"] == "李四"


def test_revise_only_when_stuck_on_feedback(pending):
    orch, tid = pending
    with pytest.raises(IllegalTransition, match="审核意见无法落实而暂停"):
        orch.revise_feedback(tid, "随便改改", user="李四")
