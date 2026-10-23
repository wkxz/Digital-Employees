"""核心模块单元测试：状态机、KPI、占位符、质检、工具网关。"""
from pathlib import Path

import pytest

from mini_employee.placeholders import fmt_value, resolve
from mini_employee.states import INITIATOR, REVIEWER, SYSTEM, S, TERMINAL, TRANSITIONS
from mini_employee.store import IllegalTransition, Store


# ---------------------------------------------------------------- 状态机

def test_every_non_terminal_state_can_be_cancelled():
    cancellable = {t.src for t in TRANSITIONS if t.event == "cancel"}
    assert cancellable == set(S) - TERMINAL


def test_terminal_states_have_no_outgoing_transitions():
    assert not [t for t in TRANSITIONS if t.src in TERMINAL]


def test_model_never_triggers_transitions():
    assert all(set(t.actors) <= {INITIATOR, REVIEWER, SYSTEM} for t in TRANSITIONS)


def test_only_reviewer_can_approve_and_only_from_pending_review():
    approves = [t for t in TRANSITIONS if t.dst == S.DELIVERED]
    assert len(approves) == 1 and approves[0].src == S.PENDING_REVIEW and approves[0].actors == (REVIEWER,)


def test_only_qc_pass_leads_to_review():
    to_review = [t for t in TRANSITIONS if t.dst == S.PENDING_REVIEW]
    assert [(t.src, t.event) for t in to_review] == [(S.QC, "qc_pass")]


def test_illegal_transition_is_rejected_and_logged():
    st = Store()
    tid = st.create_task("weekly_report", {})
    with pytest.raises(IllegalTransition):
        st.transition(tid, "approve", REVIEWER)
    assert st.get(tid)["state"] == S.CREATED
    assert st.events(tid, ["transition_rejected"])


def test_optimistic_lock():
    from mini_employee.store import ConcurrentUpdate
    st = Store()
    tid = st.create_task("weekly_report", {})
    rv = st.get(tid)["row_version"]
    st.update(tid, expect_row_version=rv, pause_reason="a")
    with pytest.raises(ConcurrentUpdate):
        st.update(tid, expect_row_version=rv, pause_reason="b")


# ---------------------------------------------------------------- KPI 与异常

def test_region_sum_equals_total(kpis):
    f = kpis["flat"]
    regions = sum(v for k, v in f.items() if k.startswith("region.") and k.endswith(".sales") and k.count(".") == 2)
    assert regions == pytest.approx(f["total.sales"])


def test_planted_anomalies_detected(kpis):
    labels = [a["label"] for a in kpis["anomalies"]]
    assert labels[0] == "华东区"
    assert {"华南区生鲜", "北京海淀店", "西南区饮料"} <= set(labels)
    east = kpis["anomalies"][0]
    assert {d["store"] for d in east["drivers"]} == {"上海徐汇店", "上海浦东店"}


def test_week_parsing_and_cross_year():
    from mini_employee.periods import parse_week
    assert parse_week("2026年第44周").key == "2026-W44"
    assert parse_week("2027-W01").prev.key == "2026-W53"
    with pytest.raises(ValueError):
        parse_week("第44周")


# ---------------------------------------------------------------- 占位符

def test_placeholder_formatting():
    flat = {"total.sales": 2512345.6, "total.sales_wow": -0.0362, "region.华东.share": 0.295}
    text, missing = resolve("销售额 {{total.sales}}，环比下降 {{total.sales_wow|abs}}（{{total.sales_wow}}），"
                            "占比 {{region.华东.share}} {{nope}}", flat)
    assert text.startswith("销售额 251.2 万元，环比下降 3.6%（-3.6%），占比 29.5%")
    assert missing == ["nope"]
    assert fmt_value("anomaly.A1.impact", -185000) == "-18.5 万元"


# ---------------------------------------------------------------- 质检

def _ctx(kpis, content, minutes=None):
    from mini_employee.qc.rules import QCContext
    from mini_employee.render import anomaly_flat
    texts = {k: [resolve(t, anomaly_flat(kpis))[0] for t in v] for k, v in content.items() if k != "anomalies"}
    return QCContext(content=content, texts=texts, unresolved=[], images=["a", "b", "c"], tables=["核心指标", "区域销售"],
                     kpis=kpis, flat=anomaly_flat(kpis), minutes=minutes or [{"no": 1, "text": "x"}],
                     sections=[{"id": "summary", "title": "摘要", "kind": "bullets"}])


def test_direction_mismatch_detected(kpis):
    from mini_employee.qc.rules import data_consistency
    ok, details, _ = data_consistency(_ctx(kpis, {"summary": ["华东区环比增长 {{region.华东.sales_wow|abs}}。"]}), {})
    assert not ok and details[0]["type"] == "direction" and details[0]["expected"] == "down"
    ok, _, _ = data_consistency(_ctx(kpis, {"summary": ["华东区环比下降 {{region.华东.sales_wow|abs}}。"]}), {})
    assert ok


def test_hardcoded_number_replaced_by_program(kpis):
    from mini_employee.qc.engine import program_fix
    from mini_employee.qc.rules import data_consistency
    from mini_employee.render import anomaly_flat
    content = {"summary": ["华东区环比下降 20.0%。"]}
    ok, details, _ = data_consistency(_ctx(kpis, content), {})
    assert not ok
    fixed, actions, remaining = program_fix(content, [{"rule": "data_consistency", "details": details}],
                                            anomaly_flat(kpis), [])
    assert "{{" in fixed["summary"][0] and not remaining and actions


def test_cause_without_evidence_must_say_pending(kpis):
    from mini_employee.qc.rules import cause_evidence
    items = [{"id": a["id"], "phenomenon": "", "cause": "竞品开业", "evidence": [], "suggestion": ""}
             for a in kpis["anomalies"]]
    ok, details, _ = cause_evidence(_ctx(kpis, {"anomalies": items}), {})
    assert not ok and all(d["type"] == "cause_missing" for d in details)
    for it in items:
        it["cause"] = "原因待确认"
    assert cause_evidence(_ctx(kpis, {"anomalies": items}), {})[0]


def test_feedback_parsing(kpis):
    from mini_employee.config import load_employee
    from mini_employee.payloads import parse_feedback
    cfg = load_employee("weekly_report").raw["feedback"]
    todos = parse_feedback("华东区下滑原因没说清楚，请结合周会纪要补充；摘要改为3条", cfg, kpis)
    assert todos[0]["section"] == "anomalies" and "华东" in todos[0]["keywords"]
    assert todos[1]["section"] == "summary" and todos[1]["max_items"] == 3


# ---------------------------------------------------------------- 工具网关

def _gateway(tmp_path, events, **kw):
    from mini_employee.tools import server
    from mini_employee.tools.gateway import ToolGateway
    return ToolGateway(server.mcp, tmp_path, lambda t, s, p: events.append((t, p)), backoff=[0], **kw)


def test_gateway_denies_tools_outside_whitelist(tmp_path):
    from mini_employee.tools.errors import ToolDenied
    ev: list = []
    gw = _gateway(tmp_path, ev)
    with pytest.raises(ToolDenied):
        gw.call("generate_report", {}, allowed=["query_kpi"], step_id="draft", caller="model")
    assert ev[0][0] == "tool_denied"
    assert [t["function"]["name"] for t in gw.model_tools(["query_kpi"])] == ["query_kpi"]
    assert "task_dir" not in gw.model_tools(["query_kpi"])[0]["function"]["parameters"]["properties"]


def test_gateway_retries_then_fails_cleanly(tmp_path):
    from mini_employee.tools.errors import TRANSIENT, ToolFailure
    ev: list = []
    gw = _gateway(tmp_path, ev, max_retries=3, task_faults=[{"tool": "search_minutes", "mode": "timeout"}])
    with pytest.raises(ToolFailure) as e:
        gw.call("search_minutes", {"keywords": ["华东"]}, allowed=["search_minutes"], step_id="analysis")
    assert e.value.kind == TRANSIENT and e.value.attempts == 4
    assert [t for t, _ in ev].count("tool_failed") == 4


def test_gateway_validates_schema(tmp_path):
    from mini_employee.tools.errors import BAD_ARGS, ToolFailure
    gw = _gateway(tmp_path, [])
    with pytest.raises(ToolFailure) as e:
        gw.call("search_minutes", {"keywords": "华东"}, allowed=["search_minutes"], step_id="x")
    assert e.value.kind == BAD_ARGS


def test_path_traversal_blocked(tmp_path):
    from mini_employee.tools.errors import BAD_ARGS, ToolFailure
    gw = _gateway(tmp_path, [])
    with pytest.raises(ToolFailure) as e:
        gw.call("read_minutes", {"file_path": "../../etc/passwd"}, allowed=["read_minutes"], step_id="x")
    assert e.value.kind == BAD_ARGS


def test_idempotent_write_does_not_duplicate(tmp_path):
    from mini_employee.fsutil import idempotent_write
    calls = []
    target = tmp_path / "out" / "周报.docx"
    w = lambda p: (calls.append(1), Path(p).write_text("x"))  # noqa: E731
    h1, reused1 = idempotent_write(target, "k1", w)
    h2, reused2 = idempotent_write(target, "k1", w)
    assert h1 == h2 and not reused1 and reused2 and len(calls) == 1
    assert sorted(p.name for p in target.parent.iterdir() if not p.name.endswith(".json")) == ["周报.docx"]


# ---------------------------------------------------------------- 端到端

def test_end_to_end_with_rework(orch, sample_files):
    tid = orch.create("weekly_report", {"period": "2026-W44"}, sample_files)
    assert orch.drive(tid) == S.PENDING_REVIEW
    orch.review(tid, "changes", "华东区下滑原因没说清楚，请结合周会纪要补充")
    t = orch.store.get(tid)
    assert t["state"] == S.PENDING_REVIEW and t["version"] == 2
    assert orch.review(tid, "approve", reviewed_sha256=t["artifact"]["sha256"]) == S.DELIVERED
    assert (orch.task_dir(tid) / "deliverable" / "经营周报_2026W44.docx").exists()
