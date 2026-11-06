"""统计周期填错（如年份写成 2025）时：提示指出可能是年份填错；补充材料时可直接改周期，不必取消重来；
只有发起人能发起任务、补充材料。"""
import importlib.util

import pytest

from mini_employee.states import REVIEWER, S
from mini_employee.store import IllegalTransition

from .conftest import ROOT


def test_period_is_normalized_and_year_typo_hinted(orch, sample_files):
    tid = orch.create("weekly_report", {"period": "2025-w44"}, sample_files)
    assert orch.store.get(tid)["form"]["period"] == "2025-W44"
    assert orch.drive(tid) == S.WAITING_INPUT
    reason = orch.store.get(tid)["pause_reason"]
    assert "不包含统计周期 2025-W44" in reason
    assert "年份是否填错" in reason and "2026-W44" in reason


def test_fix_period_on_supplement(orch, sample_files):
    tid = orch.create("weekly_report", {"period": "2025-w44"}, sample_files, user="张三")
    assert orch.drive(tid) == S.WAITING_INPUT
    assert orch.supplement(tid, None, {"period": "2026年第44周"}, user="张三") == S.PENDING_REVIEW
    t = orch.store.get(tid)
    assert t["form"]["period"] == "2026-W44"
    ev = orch.store.events(tid, ["form_changed"])
    assert ev[-1]["payload"] == {"fields": {"period": {"from": "2025-W44", "to": "2026-W44"}}, "by": "张三"}


def test_invalid_period_on_supplement_pauses_again(orch, sample_files):
    tid = orch.create("weekly_report", {"period": "2025-W44"}, sample_files)
    orch.drive(tid)
    assert orch.supplement(tid, None, {"period": "2026-W99"}) == S.WAITING_INPUT
    assert "补充的材料不符合要求" in orch.store.get(tid)["pause_reason"]


def test_supplement_rejects_unknown_fields_and_reviewer(orch, sample_files):
    tid = orch.create("weekly_report", {"period": "2025-W44"}, sample_files)
    orch.drive(tid)
    with pytest.raises(ValueError, match="不能补充的字段"):
        orch.supplement(tid, None, {"sales_file": "x.xlsx"})
    with pytest.raises(ValueError, match="不能补充的字段"):
        orch.supplement(tid, None, {"waive_sales_file": "1"})  # 销售明细不能确认跳过
    with pytest.raises(IllegalTransition):
        orch.supplement(tid, None, {"period": "2026-W44"}, actor=REVIEWER)
    assert orch.store.get(tid)["form"]["period"] == "2025-W44"
    assert not orch.store.events(tid, ["form_changed"])  # 被拒绝的操作不留下“已修改”的记录


@pytest.fixture
def web():
    spec = importlib.util.spec_from_file_location("web_server", ROOT / "app" / "web_server.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_web_only_initiator_creates(web):
    with pytest.raises(web.ApiError, match="只有发起人"):
        web.api_create({"values": {"period": "2026-W44"}, "use_sample": True, "role": "reviewer", "user": "李四"})


def test_web_supplement_can_fix_period(web, sample_files):
    o = web.orch()
    tid = o.create("weekly_report", {"period": "2025-W44"}, sample_files, user="张三")
    assert o.drive(tid) == S.WAITING_INPUT
    with pytest.raises(web.ApiError, match="不能补充的字段"):
        web.api_action(tid, "supplement", {"role": "initiator", "fields": {"foo": "bar"}})
    o.supplement(tid, None, {"period": "2026-W44"}, run=True)  # api_action 在后台线程执行，这里直接同步驱动
    assert o.store.get(tid)["state"] == S.PENDING_REVIEW
