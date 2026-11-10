"""主数据里找不到的门店 / 商品编码（新开门店、新上市商品）不能被悄悄剔除：准入时暂停，提示上传主数据；补齐后继续。"""
import pandas as pd

from mini_employee.states import S

from .conftest import SAMPLES


def _csv_with_new_store(tmp_path):
    df = pd.read_csv(SAMPLES / "销售明细.csv", encoding="utf-8-sig")
    df = df[df["销售日期"] >= "2026-10-19"].copy()  # W43–W44
    new = df[df["门店编码"] == "SH01"].copy()
    new["门店编码"], new["门店名称"] = "NJ09", "南京新店"
    new["记录ID"] = new["记录ID"].str.replace("SH01", "NJ09")
    path = tmp_path / "明细_含新店.csv"
    pd.concat([df, new]).to_csv(path, index=False, encoding="utf-8-sig")
    return path, round(new[new["销售日期"] >= "2026-10-26"]["实收金额"].sum())


def test_unknown_store_pauses_then_master_upload_resumes(orch, tmp_path):
    path, _ = _csv_with_new_store(tmp_path)
    tid = orch.create("weekly_report", {"period": "2026-W44"},
                      {"sales_file": str(path), "minutes_file": str(SAMPLES / "周会纪要_2026W44.docx")}, user="张三")
    orch.drive(tid)
    t = orch.store.get(tid)
    assert t["state"] == S.WAITING_INPUT
    assert "NJ09" in t["pause_reason"] and "门店信息中找不到" in t["pause_reason"] and "主数据" in t["pause_reason"]

    book = pd.read_excel(SAMPLES / "销售明细.xlsx", sheet_name=None, engine="calamine")
    stores = pd.concat([book["门店信息"], pd.DataFrame([{"门店编码": "NJ09", "门店名称": "南京新店", "区域": "华东", "城市": "南京",
                                                    "店型": "社区店", "面积(㎡)": 80, "开业日期": "2026-10-01"}])])
    master = tmp_path / "主数据.xlsx"
    with pd.ExcelWriter(master) as w:
        stores.to_excel(w, sheet_name="门店信息", index=False)
    orch.supplement(tid, {"master_file": str(master)}, {}, user="张三")
    assert orch.store.get(tid)["state"] == S.PENDING_REVIEW


def test_missing_target_week_pauses(orch):
    """销售目标表没有当周目标时算不出目标达成率，暂停请补主数据（官方数据目标只到 W46，W44 正常）。"""
    from mini_employee.orchestrator import Orchestrator
    from mini_employee.periods import parse_week
    info = {"target_weeks": ["W44", "W45", "W46"], "unmatched_rows": {}}
    assert Orchestrator._master_gap(parse_week("2026-W44"), info) is None
    msg = Orchestrator._master_gap(parse_week("2026-W47"), info)
    assert "没有第 47 周的目标" in msg
    gap = {"商品信息": {"C07": {"rows": 3, "weeks": ["2026-W40"], "sales": 96.0}}}
    assert Orchestrator._master_gap(parse_week("2026-W44"), {"unmatched_rows": gap}) is None  # 只影响别的周，不拦
