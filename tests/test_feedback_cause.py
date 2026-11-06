"""“某区域下滑原因没说清楚，请结合周会纪要补充”这类意见：定位到具体异常条目和纪要中相关的条目，
逐条核对原因是否真的补充了依据（只换说法不算）；同一条意见再次出现时记录第几次提出。
实测中同一条意见退回 3 次，原检查只看“异常章节有变化且提到华东”，每次都判为已采纳。"""
from types import SimpleNamespace

import pytest

from mini_employee.fsutil import read_json
from mini_employee.payloads import parse_feedback
from mini_employee.qc.rules import feedback_addressed
from mini_employee.states import S

COMMENT = "华东区下滑原因没说清楚，请结合周会纪要补充"


@pytest.fixture
def done(orch, sample_files):
    tid = orch.create("weekly_report", {"period": "2026-W44"}, sample_files)
    assert orch.drive(tid) == S.PENDING_REVIEW
    d = orch.task_dir(tid)
    return orch, tid, read_json(d / "work" / "kpis.json"), read_json(d / "work" / "minutes.json")


def _cfg():
    from mini_employee.config import load_employee
    return load_employee("weekly_report").raw["feedback"]


def test_cause_feedback_targets_anomaly_and_related_minutes(done):
    _, _, kpis, minutes = done
    todo = parse_feedback(COMMENT, _cfg(), kpis, minutes)[0]
    assert todo["section"] == "anomalies" and todo["need_minutes"]
    t = todo["targets"][0]
    assert t["label"] == "华东区"
    # 第 1 条提到华东、第 2 条是华东负责人发言、第 8 条是静安店（带动门店）的行动项；华南、华北的条目不算
    assert {1, 2, 8} <= set(t["minutes"]) and 3 not in t["minutes"]
    assert "repeat" not in todo
    again = parse_feedback(COMMENT, _cfg(), kpis, minutes, history=[COMMENT, "摘要改为3条；" + COMMENT])[0]
    assert again["repeat"] == 3


def test_non_cause_feedback_keeps_old_behavior(done):
    _, _, kpis, minutes = done
    todo = parse_feedback("摘要改为3条", _cfg(), kpis, minutes)[0]
    assert todo["section"] == "summary" and "targets" not in todo


def _check(todo, prev_item, cur_item):
    ctx = SimpleNamespace(content={"anomalies": [cur_item]}, prev_content={"anomalies": [prev_item]},
                          todos=[todo], texts={})
    return feedback_addressed(ctx, {})


TODO = {"section": "anomalies", "instruction": COMMENT, "keywords": ["华东"], "metrics": {},
        "targets": [{"id": "A1", "label": "华东区", "minutes": [1, 2, 8]}], "need_minutes": True}


def test_rephrasing_without_new_evidence_is_not_adopted():
    prev = {"id": "A1", "cause": "湖滨店停业三天；静安店对面竞品开业。", "evidence": [1, 2]}
    cur = {"id": "A1", "cause": "受湖滨店停业三天、静安店对面竞品开业影响。", "evidence": [1, 2]}
    ok, details, _ = _check(TODO, prev, cur)
    assert not ok and "没有补充新的纪要依据" in details[0]["msg"] and "第 8 条" in details[0]["msg"]


def test_citing_new_related_minutes_is_adopted():
    prev = {"id": "A1", "cause": "湖滨店停业三天；静安店对面竞品开业。", "evidence": [1, 2]}
    cur = {"id": "A1", "cause": "湖滨店停业三天；静安店对面竞品开业，区域已安排静安店会员日活动应对。", "evidence": [1, 2, 8]}
    ok, details, _ = _check(TODO, prev, cur)
    assert ok and not details


def test_unchanged_cause_is_not_adopted_even_when_all_minutes_cited():
    item = {"id": "A1", "cause": "湖滨店停业三天；静安店对面竞品开业。", "evidence": [1, 2, 8]}
    ok, details, _ = _check(TODO, item, dict(item))
    assert not ok and "与上一版相同" in details[0]["msg"]


def test_no_more_minutes_passes_with_note_for_reviewer():
    prev = {"id": "A1", "cause": "湖滨店停业三天；静安店对面竞品开业。", "evidence": [1, 2, 8]}
    cur = {"id": "A1", "cause": "湖滨店停业三天、静安店受对面竞品开业分流，纪要中暂无更多说明。", "evidence": [1, 2, 8]}
    ok, details, _ = _check(TODO, prev, cur)
    assert ok and details[0]["type"] == "note" and "已全部引用" in details[0]["msg"]


def test_review_records_repeat_and_targets(done):
    orch, tid, _, _ = done
    orch.review(tid, "changes", COMMENT, actor="reviewer")
    assert orch.store.get(tid)["state"] == S.PENDING_REVIEW
    # 第 2 版已引用全部相关纪要：再次退回前先提示审核人，确认后仍可退回
    from mini_employee.payloads import FeedbackCheck
    with pytest.raises(FeedbackCheck, match="已全部引用"):
        orch.review(tid, "changes", COMMENT, actor="reviewer")
    assert orch.store.get(tid)["state"] == S.PENDING_REVIEW and orch.store.get(tid)["rework_rounds"] == 1
    orch.review(tid, "changes", COMMENT, actor="reviewer", confirm=True)
    todos = orch.store.events(tid, ["feedback_parsed"])[-1]["payload"]["todos"]
    assert todos[0]["repeat"] == 2 and todos[0]["targets"][0]["id"] == "A1"
    # Mock 模型第二次返工写出与上一版相同的原因：判为未采纳（原检查同样不放行），不会当作已落实送审
    rounds = read_json(orch.task_dir(tid) / "qc" / "qc_report.json")["rounds"]
    first_v3 = next(r for r in rounds if r["version"] == 3)
    fa = next(r for r in first_v3["results"] if r["rule"] == "feedback_addressed")
    assert not fa["passed"] and "与上一版相同" in fa["details"][0]["msg"]
    print(orch.store.get(tid)["state"], orch.store.get(tid)["pause_reason"])
