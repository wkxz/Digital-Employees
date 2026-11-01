"""配置权限与留痕：只有管理员能改配置、每次修改可追溯可回退、进行中的任务不受配置修改影响、不能审核自己发起的任务。"""
import shutil
from pathlib import Path

import pytest

from mini_employee import config_admin as CA
from mini_employee.states import S
from mini_employee.store import IllegalTransition

ROOT = Path(__file__).resolve().parents[1]
EMP = "weekly_report"


@pytest.fixture
def employees(tmp_path, monkeypatch):
    import mini_employee.config as C
    base = tmp_path / "employees"
    shutil.copytree(ROOT / "employees", base)
    monkeypatch.setattr(C, "EMPLOYEES_DIR", base)
    monkeypatch.setenv("MINI_ADMIN_PASSWORD", "secret")
    return base / EMP


def _yaml(d: Path) -> str:
    return (d / "employee.yaml").read_text(encoding="utf-8")


def test_only_admin_can_save(employees, orch):
    new = _yaml(employees).replace("anomaly_threshold: 0.10", "anomaly_threshold: 0.15")
    for pw in (None, "wrong"):
        errors, _, seq = CA.save(orch.store, EMP, "employee.yaml", new, "王五", pw)
        assert errors and "管理员" in errors[0] and seq is None
    assert "0.10" in _yaml(employees)
    errors, _, seq = CA.save(orch.store, EMP, "employee.yaml", new, "王五", "secret", "阈值放宽")
    assert not errors and seq and "anomaly_threshold: 0.15" in _yaml(employees)


def test_no_admin_password_configured_means_read_only(employees, orch, monkeypatch):
    monkeypatch.delenv("MINI_ADMIN_PASSWORD")
    errors, _, _ = CA.save(orch.store, EMP, "employee.yaml", _yaml(employees) + "\n", "王五", "")
    assert errors


def test_cannot_write_other_files(employees, orch):
    errors, _, _ = CA.save(orch.store, EMP, "../../evil.py", "x", "王五", "secret")
    assert errors and "不允许" in errors[0]


def test_invalid_config_is_not_saved_or_recorded(employees, orch):
    bad = _yaml(employees).replace("anomaly_threshold: 0.10", "anomaly_threshold: 15")
    errors, _, seq = CA.save(orch.store, EMP, "employee.yaml", bad, "王五", "secret")
    assert errors and seq is None and "0.10" in _yaml(employees)
    assert all(c["user"] == CA.BASELINE for c in CA.history(orch.store, EMP))


def test_history_records_who_what_and_rollback(employees, orch):
    old = _yaml(employees)
    new = old.replace("anomaly_threshold: 0.10", "anomaly_threshold: 0.15")
    _, _, seq = CA.save(orch.store, EMP, "employee.yaml", new, "王五", "secret", "阈值放宽")
    ch = orch.store.get_config_change(seq)
    assert ch["user"] == "王五" and ch["note"] == "阈值放宽" and ch["content_before"] == old and ch["content_after"] == new
    errors, _, back = CA.rollback(orch.store, EMP, seq, "王五", "secret")
    assert not errors and _yaml(employees) == old
    assert orch.store.get_config_change(back)["note"] == f"回退变更 #{seq}"
    errors, _, _ = CA.rollback(orch.store, EMP, seq, "赵六", None)  # 回退同样需要管理员
    assert errors


def test_direct_file_edit_is_detected(employees, orch):
    CA.record_external_changes(orch.store, EMP)  # 首次记录
    (employees / "SKILL.md").write_text((employees / "SKILL.md").read_text(encoding="utf-8") + "\n- 新增一条要求。\n",
                                        encoding="utf-8")
    CA.record_external_changes(orch.store, EMP)
    last = orch.store.last_config_change(EMP, "SKILL.md")
    assert last["user"] == CA.EXTERNAL and "新增一条要求" in last["content_after"]


def test_running_task_uses_config_snapshot(employees, orch, sample_files):
    tid = orch.create(EMP, {"period": "2026-W44"}, sample_files, {"faults": [{"tool": "llm", "mode": "timeout"}]})
    sha = orch.store.get(tid)["meta"]["config_sha"]
    assert (orch.task_dir(tid) / "config" / EMP / "employee.yaml").exists()
    assert orch.drive(tid) == S.SUSPENDED
    new = _yaml(employees).replace("total_max_chars: 4000", "total_max_chars: 10")  # 期间管理员改了配置（字数上限改得很严）
    assert new != _yaml(employees)
    CA.save(orch.store, EMP, "employee.yaml", new, "王五", "secret")
    assert orch.retry(tid, clear_faults=True) == S.PENDING_REVIEW  # 仍按发起时的配置执行
    assert orch.store.get(tid)["meta"]["config_sha"] == sha
    later = orch.create(EMP, {"period": "2026-W44"}, sample_files)
    assert orch.store.get(later)["meta"]["config_sha"] != sha  # 新任务用新配置


def test_initiator_cannot_review_own_task(orch, sample_files):
    tid = orch.create(EMP, {"period": "2026-W44"}, sample_files, user="张三")
    assert orch.drive(tid) == S.PENDING_REVIEW
    sha = orch.store.get(tid)["artifact"]["sha256"]
    for decision, comments in (("approve", ""), ("changes", "华东区原因请补充")):
        with pytest.raises(IllegalTransition, match="不能审核自己发起的任务"):
            orch.review(tid, decision, comments, reviewed_sha256=sha, user="张三")
    assert orch.store.get(tid)["state"] == S.PENDING_REVIEW
    assert not (orch.task_dir(tid) / "steps" / "feedback@v2.json").exists()  # 被拒绝的退回没有留下审核意见
    rejected = orch.store.events(tid, ["transition_rejected"])
    assert len(rejected) == 2 and all("不能审核自己" in e["payload"]["reason"] for e in rejected)
    assert orch.review(tid, "approve", reviewed_sha256=sha, user="李四") == S.DELIVERED


def test_state_machine_guard_also_blocks_self_review(orch, sample_files):
    """即使绕过 review() 直接调用状态流转，闸门也会拦下。"""
    tid = orch.create(EMP, {"period": "2026-W44"}, sample_files, user="张三")
    orch.drive(tid)
    sha = orch.store.get(tid)["artifact"]["sha256"]
    with pytest.raises(IllegalTransition, match="不能审核自己"):
        orch.store.transition(tid, "approve", "reviewer", {"reviewed_sha256": sha, "user": "张三"})
