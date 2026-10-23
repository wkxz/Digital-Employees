import os
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SAMPLES = ROOT / "data" / "samples"


@pytest.fixture(autouse=True)
def isolated_runs(tmp_path, monkeypatch):
    monkeypatch.setenv("MINI_RUNS", str(tmp_path / "runs"))
    monkeypatch.setenv("MINI_TODAY", "2026-11-03")
    monkeypatch.setenv("MINI_FAST", "1")
    monkeypatch.delenv("MINI_FAULTS", raising=False)
    monkeypatch.setenv("MINI_MODEL", "mock")  # 单元测试固定用 Mock；真实模型用 mini cases 跑
    yield tmp_path


@pytest.fixture
def orch():
    from mini_employee.orchestrator import Orchestrator
    return Orchestrator()


@pytest.fixture
def sample_files():
    return {"sales_file": str(SAMPLES / "销售明细_2026W44.xlsx"), "minutes_file": str(SAMPLES / "周会纪要_2026W44.docx")}


@pytest.fixture
def kpis():
    import pandas as pd

    from mini_employee.periods import parse_week
    from mini_employee.tools import data as D

    df, _ = D.load_sales(SAMPLES / "销售明细_2026W44.xlsx", {}, ["日期", "门店", "区域", "品类", "销售额", "销量"])
    df, _ = D.clean_sales(df)
    return D.compute_kpis(df, parse_week("2026-W44"), 0.15, 5, 20000, ["region", "store", "category", "rc"])


os.environ.setdefault("MINI_FAST", "1")
