"""把 cases/*.yaml 中的全部用例作为 pytest 用例运行。"""
import pytest

from mini_employee.runner import load_cases, run_case

CASES = load_cases(["cases"])


@pytest.mark.parametrize("case", CASES, ids=[c["id"] for c in CASES])
def test_case(case, tmp_path, monkeypatch):
    for k in ("MINI_RUNS", "MINI_TODAY"):
        monkeypatch.setenv(k, "")
    monkeypatch.setenv("MINI_MODEL", "mock")
    run_case(case, tmp_path / case["id"])
