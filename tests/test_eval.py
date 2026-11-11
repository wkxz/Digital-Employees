"""对比评测（mini eval）与可选的 LangSmith 追踪。"""
import yaml

from mini_employee import evaluate, tracing
from mini_employee.config import EMPLOYEES_DIR


def test_variant_patches_only_a_copy(tmp_path):
    original = (EMPLOYEES_DIR / "weekly_report" / "SKILL.md").read_text(encoding="utf-8")
    env = evaluate.prepare_variant({"name": "改版", "env": {"MINI_MODEL_NAME": "x"},
                                    "skill_patch": [{"old": "不超过 5 条", "new": "不超过 4 条"}],
                                    "employee": {"model.temperature": 0.7}}, tmp_path, EMPLOYEES_DIR)
    copy = tmp_path / "employees" / "weekly_report"
    assert env["MINI_EMPLOYEES"] == str(tmp_path / "employees") and env["MINI_MODEL_NAME"] == "x"
    assert "不超过 4 条" in (copy / "SKILL.md").read_text(encoding="utf-8")
    assert yaml.safe_load((copy / "employee.yaml").read_text(encoding="utf-8"))["model"]["temperature"] == 0.7
    assert (EMPLOYEES_DIR / "weekly_report" / "SKILL.md").read_text(encoding="utf-8") == original, "正式配置不应被改动"
    assert evaluate.prepare_variant({"name": "只换模型", "env": {"A": 1}}, tmp_path / "b", EMPLOYEES_DIR) == {"A": "1"}


def test_bad_patch_fails_before_running(tmp_path):
    import pytest
    with pytest.raises(ValueError, match="找不到"):
        evaluate.prepare_variant({"name": "错", "skill_patch": [{"old": "不存在的文字", "new": "x"}]}, tmp_path, EMPLOYEES_DIR)


def test_eval_runs_each_variant_and_writes_report(tmp_path, monkeypatch):
    monkeypatch.setenv("MINI_MODEL", "mock")
    spec = tmp_path / "spec.yaml"
    spec.write_text(yaml.safe_dump({"cases": ["A01"], "jobs": 1, "variants": [
        {"name": "当前配置"},
        {"name": "摘要 4 条", "skill_patch": [{"old": "summary（本周摘要）：不超过 5 条", "new": "summary（本周摘要）：不超过 4 条"}]},
    ]}, allow_unicode=True), encoding="utf-8")
    report = tmp_path / "eval.md"
    assert evaluate.run_eval(str(spec), str(report)) == 0
    text = report.read_text(encoding="utf-8")
    assert "| 当前配置 | mock | 1/1 |" in text and "| 摘要 4 条 | mock | 1/1 |" in text
    assert "岗位说明“summary（本周摘要）：不超过 5 条”→“summary（本周摘要）：不超过 4 条”" in text
    assert "| A01 |" in text and "k tok" in text


def test_metrics_count_tokens_qc_and_rework(orch, sample_files, tmp_path):
    from mini_employee.states import S
    tid = orch.create("weekly_report", {"period": "2026-W44"}, sample_files, user="张三")
    assert orch.drive(tid) == S.PENDING_REVIEW
    m = evaluate.collect_metrics(orch.store.path.parent)
    assert m["model_calls"] >= 2 and m["tokens"] > 0 and m["versions"] == 1 and m["qc_first_pass"] == 1
    assert m["rework"] == 0 and m["delivered"] == 0


def test_tracing_is_off_by_default_and_wraps_client_when_on(monkeypatch):
    for k in ("LANGSMITH_TRACING", "LANGCHAIN_TRACING_V2", "LANGSMITH_API_KEY", "LANGCHAIN_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    sentinel = object()
    assert not tracing.enabled() and tracing.wrap_client(sentinel) is sentinel
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    assert not tracing.enabled(), "没有 API Key 时不开启"
    monkeypatch.setenv("LANGSMITH_API_KEY", "lsv2-test")
    assert tracing.enabled()
    import openai
    client = openai.OpenAI(api_key="x", base_url="http://127.0.0.1:9/v1")
    wrapped = tracing.wrap_client(client)
    assert getattr(wrapped.chat.completions.create, "__wrapped__", None) is not None, "模型调用应被 LangSmith 包装"
    cfg = tracing.run_config("T1", "weekly_report")
    assert cfg["metadata"] == {"task_id": "T1", "employee": "weekly_report"} and "weekly_report" in cfg["tags"]
