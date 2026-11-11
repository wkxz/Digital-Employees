"""人工在线修改报告：待审核 / 暂停转人工时可在网页上修改文字，新版本必须重新质检，再经审核才能交付。"""
import importlib.util

import pytest

from mini_employee.fsutil import read_json
from mini_employee.states import S
from mini_employee.store import IllegalTransition

from .conftest import ROOT


@pytest.fixture
def web():
    spec = importlib.util.spec_from_file_location("web_server", ROOT / "app" / "web_server.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _pending(o, sample_files):
    tid = o.create("weekly_report", {"period": "2026-W44"}, sample_files, user="张三")
    assert o.drive(tid) == S.PENDING_REVIEW
    return tid


def test_content_api_lists_editable_sections(web, sample_files):
    o = web.orch()
    tid = _pending(o, sample_files)
    data = web.api_content(tid)
    assert data["editable"] and data["state"] == S.PENDING_REVIEW
    ids = [s["id"] for s in data["sections"]]
    assert "summary" in ids and "anomalies" not in ids
    assert data["content"]["anomalies"] and data["anomaly_labels"]


def test_preview_resolves_placeholders_and_flags_unknown(web, sample_files):
    o = web.orch()
    tid = _pending(o, sample_files)
    r = web.api_preview(tid, {"content": {"summary": ["实收 {{total.sales}}，{{total.not_a_key}}"],
                                          "anomalies": [{"id": "A1", "cause": "环比 {{region.华东.sales_wow|abs}}"}]}})
    assert "元" in r["texts"]["summary"][0] and "{{total.sales}}" not in r["texts"]["summary"][0]
    assert r["missing"] == ["total.not_a_key"]
    assert "%" in r["anomalies"]["A1"]["cause"]


def test_edit_before_review_rechecks_new_version(web, sample_files):
    o = web.orch()
    tid = _pending(o, sample_files)
    old = o.store.get(tid)["artifact"]
    content = web.api_content(tid)["content"]
    content["summary"].append("   ")  # 空段落会被去掉
    content["next_week"][-1] = "人工补充：跟进华东会员日活动效果。"
    o.manual_recheck(tid, web.clean_content(content), actor="reviewer")
    t = o.store.get(tid)
    assert t["state"] == S.PENDING_REVIEW, "修改后的版本须重新质检，通过后回到待审核"
    assert t["artifact"]["sha256"] != old["sha256"]
    final = read_json(o.task_dir(tid) / t["artifact"]["content_path"])
    assert "人工补充：跟进华东会员日活动效果。" in final["next_week"]
    assert all(x.strip() for x in final["summary"])
    # 审核人必须审核新版本：用旧版本的哈希不能通过
    with pytest.raises(IllegalTransition):
        o.review(tid, "approve", reviewed_sha256=old["sha256"], actor="reviewer", user="李四")
    o.review(tid, "approve", reviewed_sha256=t["artifact"]["sha256"], actor="reviewer", user="李四")
    assert o.store.get(tid)["state"] == S.DELIVERED


def test_edit_with_wrong_number_does_not_skip_qc(web, sample_files):
    o = web.orch()
    tid = _pending(o, sample_files)
    content = web.api_content(tid)["content"]
    content["summary"][0] = "全公司实收 999,999 元，环比大幅增长。"
    o.manual_recheck(tid, web.clean_content(content))
    t = o.store.get(tid)
    assert t["state"] == S.SUSPENDED, "手写的错误数字不能送审，交回人工修改"
    assert "人工修改后的版本质检未通过" in t["pause_reason"]
    assert web.api_content(tid)["editable"]


def test_failed_manual_edit_is_not_rewritten_by_model(web, sample_files):
    o = web.orch()
    tid = _pending(o, sample_files)
    content = web.api_content(tid)["content"]
    marker = "人工补充说明：" + "华东会员日活动需要逐店跟进执行情况，" * 30
    content["summary"].append(marker)
    o.manual_recheck(tid, web.clean_content(content))
    t = o.store.get(tid)
    assert t["state"] == S.SUSPENDED
    assert not any(e["step_id"] == "fix" for e in o.store.events(tid, ["model_called"])), "不应调用模型改写人工修改"
    final = read_json(o.task_dir(tid) / t["artifact"]["content_path"])
    assert marker in final["summary"], "人工修改的内容应保留，交回人工继续修改"


def test_edit_not_allowed_after_delivery(web, sample_files):
    o = web.orch()
    tid = _pending(o, sample_files)
    t = o.store.get(tid)
    o.review(tid, "approve", reviewed_sha256=t["artifact"]["sha256"], actor="reviewer", user="李四")
    assert not web.api_content(tid)["editable"]
    with pytest.raises(IllegalTransition):
        o.manual_recheck(tid, read_json(o.task_dir(tid) / t["artifact"]["content_path"]))


def test_bad_content_is_rejected(web, sample_files):
    o = web.orch()
    tid = _pending(o, sample_files)
    with pytest.raises(web.ApiError):
        web.clean_content(["not", "a", "dict"])
    with pytest.raises(web.ApiError, match="格式不正确"):
        web.api_action(tid, "recheck", {"role": "initiator", "content": {"summary": ["只有摘要"]}})
    assert o.store.get(tid)["state"] == S.PENDING_REVIEW
