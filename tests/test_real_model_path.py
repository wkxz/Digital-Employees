"""用本地假 OpenAI 服务测试真实模型的代码路径（HTTP + openai SDK + tool_calls）。"""
import pytest

from mini_employee.runner import load_cases, run_case

from . import fake_openai


@pytest.fixture
def fake_server(monkeypatch):
    url, srv = fake_openai.start()
    fake_openai._Handler.calls.clear()
    monkeypatch.setenv("MINI_MODEL", "openai_compatible")
    monkeypatch.setenv("MINI_BASE_URL", url)
    monkeypatch.setenv("MINI_API_KEY", "test-key")
    monkeypatch.setenv("MINI_MODEL_NAME", "fake-model")
    yield url
    srv.shutdown()


def test_check_model(fake_server, capsys):
    from mini_employee.llm.check import check_model
    assert check_model() == 0
    assert "3/3" in capsys.readouterr().out


def test_wrong_key_pauses_with_clear_reason(fake_server, monkeypatch, orch, sample_files):
    from mini_employee.states import S
    monkeypatch.setenv("MINI_API_KEY", "wrong")
    tid = orch.create("weekly_report_demo", {"period": "2026-W44"}, sample_files)
    assert orch.drive(tid) == S.SUSPENDED
    assert "401" in orch.store.get(tid)["pause_reason"]


@pytest.mark.parametrize("cid", ["N01", "U01", "F02", "C01"])
def test_cases_over_http(fake_server, cid, tmp_path, monkeypatch):
    case = next(c for c in load_cases(["cases"]) if c["id"] == cid)
    monkeypatch.setenv("MINI_RUNS", "")
    run_case(case, tmp_path / cid)
    purposes = {c["purpose"] for c in fake_openai._Handler.calls}
    assert {"analysis", "draft"} <= purposes
    assert all("generate_report" not in c["tools"] for c in fake_openai._Handler.calls)  # 模型只看到开放的工具
