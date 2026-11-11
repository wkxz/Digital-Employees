"""复核员：另一个模型逐条核对原因与所引纪要是否相符（质检规则 cause_review）。"""
import copy

from mini_employee.fsutil import read_json
from mini_employee.llm import resolved_config
from mini_employee.states import S


def _rounds(orch, tid):
    return read_json(orch.task_dir(tid) / "qc" / "qc_report.json")["rounds"]


def _result(rnd, rule):
    return next(r for r in rnd["results"] if r["rule"] == rule)


def test_reviewer_checks_cited_causes_and_caches(orch, sample_files):
    tid = orch.create("weekly_report", {"period": "2026-W44"}, sample_files, user="张三")
    assert orch.drive(tid) == S.PENDING_REVIEW
    r = _result(_rounds(orch, tid)[-1], "cause_review")
    assert r["passed"] and r["severity"] == "warning"
    assert "均有依据" in r["details"][0]["msg"] and "mock" in r["details"][0]["msg"]
    calls = [e for e in orch.store.events(tid, ["model_called"]) if e["step_id"] == "verify"]
    assert len(calls) == 1
    cached = list((orch.task_dir(tid) / "work" / "cause_review").glob("*.json"))
    assert cached, "复核结论应按条目缓存"
    orch.store.transition(tid, "manual_recheck", "initiator", {"reason": "测试：重新质检"})
    assert orch.drive(tid) == S.PENDING_REVIEW
    calls = [e for e in orch.store.events(tid, ["model_called"]) if e["step_id"] == "verify"]
    assert len(calls) == 1, "内容未变时重新质检不应再次调用复核员"


def test_unsupported_causes_warn_reviewer_without_blocking(orch, sample_files):
    tid = orch.create("weekly_report", {"period": "2026-W44"}, sample_files,
                      {"faults": [{"tool": "mock", "mode": "review_reject"}]}, user="张三")
    assert orch.drive(tid) == S.PENDING_REVIEW  # warning：只提示审核人，不进入自动修正
    r = _result(_rounds(orch, tid)[-1], "cause_review")
    assert not r["passed"] and all(d["type"] == "cause_unsupported" for d in r["details"])
    assert "复核员认为" in r["details"][0]["msg"]


def test_blocking_reviewer_sends_unsupported_causes_to_auto_fix(orch, sample_files, monkeypatch):
    from mini_employee import orchestrator as om
    real = om.Orchestrator._emp

    def emp(self, task):
        e = real(self, task)
        e = copy.copy(e)
        e.quality_checks = [{**c, "severity": "blocking"} if c["rule"] == "cause_review" else c
                            for c in e.quality_checks]
        return e

    monkeypatch.setattr(om.Orchestrator, "_emp", emp)
    tid = orch.create("weekly_report", {"period": "2026-W44"}, sample_files,
                      {"faults": [{"tool": "mock", "mode": "review_reject"}]}, user="张三")
    st = orch.drive(tid)
    path = [e["payload"]["to"] for e in orch.store.events(tid, ["state_changed"])]
    assert "AUTO_FIXING" in path, "blocking 时复核不通过应进入自动修正"
    assert st in (S.PENDING_REVIEW, S.SUSPENDED)


def test_reviewer_unavailable_does_not_block(orch, sample_files, monkeypatch):
    from mini_employee.llm import LLMUnavailable
    from mini_employee import orchestrator as om

    def boom(*a, role=None, **k):
        if role == "reviewer":
            raise LLMUnavailable("复核模型未配置")
        return real(*a, **k)

    real = om.build_llm
    monkeypatch.setattr(om, "build_llm", boom)
    tid = orch.create("weekly_report", {"period": "2026-W44"}, sample_files, user="张三")
    assert orch.drive(tid) == S.PENDING_REVIEW
    r = _result(_rounds(orch, tid)[-1], "cause_review")
    assert r["passed"] and "未能完成复核" in r["details"][0]["msg"]


def test_reviewer_model_config(monkeypatch):
    for k in ("MINI_MODEL", "MINI_BASE_URL", "MINI_MODEL_NAME", "MINI_API_KEY", "MINI_REVIEW_MODEL",
              "MINI_REVIEW_MODEL_NAME", "MINI_REVIEW_BASE_URL", "MINI_REVIEW_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    cfg = {"provider": "deepseek", "temperature": 0.2}
    assert resolved_config(cfg, "reviewer") == resolved_config(cfg), "未配置复核员时与主模型相同"
    monkeypatch.setenv("MINI_MODEL_NAME", "main-model")
    monkeypatch.setenv("MINI_REVIEW_MODEL_NAME", "review-model")
    assert resolved_config(cfg)["model"] == "main-model"
    assert resolved_config(cfg, "reviewer")["model"] == "review-model"
    monkeypatch.delenv("MINI_REVIEW_MODEL_NAME")
    r = resolved_config({**cfg, "reviewer": {"provider": "qwen"}}, "reviewer")
    assert r["provider"] == "qwen" and r["model"] == "qwen-plus" and "dashscope" in r["base_url"], "换服务商时不沿用主模型地址"
    assert r["temperature"] == 0.2
