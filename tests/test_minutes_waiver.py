"""周会纪要默认必填：缺了暂停；发起人明确确认“本周没有纪要”才照常生成（原因标注待核实、留痕）。
以及新版 Web 控制台（app/web_server.py）的权限：保存配置、全局故障须管理员口令，发起人不能审核自己发起的任务。"""
import importlib.util

import pytest

from .conftest import ROOT, SAMPLES
from mini_employee.states import S
from mini_employee.store import IllegalTransition

SALES_ONLY = {"sales_file": str(SAMPLES / "销售明细.xlsx")}


def _report(orch, tid):
    from mini_employee.runner import _report_text
    return _report_text(orch, tid)


def test_missing_minutes_pauses_and_offers_waiver(orch):
    tid = orch.create("weekly_report", {"period": "2026-W44"}, SALES_ONLY)
    assert orch.drive(tid) == S.WAITING_INPUT
    reason = orch.store.get(tid)["pause_reason"]
    assert "缺少材料：周会纪要" in reason and "本周没有周会纪要" in reason


def test_waiver_at_create_generates_with_pending_causes(orch):
    tid = orch.create("weekly_report", {"period": "2026-W44", "waive_minutes_file": "1"}, SALES_ONLY, user="张三")
    assert orch.drive(tid) == S.PENDING_REVIEW
    text = _report(orch, tid)
    assert "原因待核实" in text and "发起人确认本周没有周会纪要" in text
    ev = orch.store.events(tid, ["material_waived"])
    assert len(ev) == 1 and ev[0]["payload"]["by"] == "张三" and ev[0]["payload"]["fields"] == ["周会纪要"]


def test_waiver_on_supplement_records_who_confirmed(orch):
    tid = orch.create("weekly_report", {"period": "2026-W44"}, SALES_ONLY, user="张三")
    assert orch.drive(tid) == S.WAITING_INPUT
    assert orch.supplement(tid, None, {"waive_minutes_file": "1"}, user="王五") == S.PENDING_REVIEW
    assert orch.store.events(tid, ["material_waived"])[0]["payload"]["by"] == "王五"


def test_only_waivable_materials_can_be_skipped(orch):
    tid = orch.create("weekly_report", {"period": "2026-W44", "waive_sales_file": "1"},
                      {"minutes_file": str(SAMPLES / "周会纪要_2026W44.docx")})
    assert orch.drive(tid) == S.WAITING_INPUT
    assert "缺少材料：销售明细" in orch.store.get(tid)["pause_reason"]


def test_waivable_only_on_required_file_fields():
    from mini_employee.config import parse_skill_md, read_employee_files
    from mini_employee.validate import validate
    _, raw, skill = read_employee_files("weekly_report")
    raw["form"][0]["waivable"] = "没有统计周期"  # period 不是文件
    errors, _ = validate(raw, parse_skill_md(skill)[1])
    assert any("waivable 只用于必填的 file 字段" in e for e in errors)


# ---------------------------------------------------------------- 新版 Web 控制台的权限

@pytest.fixture
def web(monkeypatch):

    spec = importlib.util.spec_from_file_location("web_server", ROOT / "app" / "web_server.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setenv("MINI_ADMIN_PASSWORD", "secret")
    return mod


def test_web_config_save_requires_admin(web):
    from mini_employee.config import EMPLOYEES_DIR
    path = EMPLOYEES_DIR / "weekly_report" / "employee.yaml"
    text = path.read_text(encoding="utf-8")
    res = web.api_config_save({"name": "employee.yaml", "text": text, "save": False})
    assert not res["errors"] and not res["saved"]  # 只校验：任何人可用
    for pw in (None, "wrong"):
        res = web.api_config_save({"name": "employee.yaml", "text": text + "\n", "admin_password": pw, "user": "张三"})
        assert not res["saved"] and "管理员" in res["errors"][0]
    assert path.read_text(encoding="utf-8") == text


def test_web_global_faults_require_admin(web):
    with pytest.raises(web.ApiError, match="管理员口令"):
        web.api_faults_set({"ids": ["llm:timeout"]})
    assert web.api_faults_set({"ids": ["llm:timeout"], "admin_password": "secret"}) == [{"tool": "llm", "mode": "timeout"}]
    web.api_faults_set({"ids": [], "admin_password": "secret"})


def test_web_initiator_cannot_review_own_task(web, sample_files):
    o = web.orch()
    tid = o.create("weekly_report", {"period": "2026-W44"}, sample_files, user="张三")
    assert o.drive(tid) == S.PENDING_REVIEW
    sha = o.store.get(tid)["artifact"]["sha256"]
    with pytest.raises(IllegalTransition, match="不能审核自己"):
        web.api_action(tid, "review", {"decision": "approve", "sha256": sha, "role": "reviewer", "user": "张三"})
    web.api_action(tid, "review", {"decision": "approve", "sha256": sha, "role": "reviewer", "user": "李四"})
    assert o.store.get(tid)["state"] == S.DELIVERED
