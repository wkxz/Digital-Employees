"""场景 A（青禾烘焙官方数据）：多表关联、去重、退货、目标达成率、缺数日期、纪要解析与核对、篇幅估算。

端到端的判定要点见 cases/A01–A06.yaml；这里覆盖各环节的单元行为。数字均已用销售明细独立复算。
"""
from pathlib import Path

import pytest
import yaml

from mini_employee.periods import parse_week
from mini_employee.reconcile import check, claims
from mini_employee.tools import data as D

ROOT = Path(__file__).resolve().parents[1]
A = ROOT / "data" / "official_a"
EMP = ROOT / "employees" / "weekly_report"


@pytest.fixture(scope="module")
def cfg():
    return yaml.safe_load((EMP / "employee.yaml").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def loaded(cfg):
    """只上传 CSV 明细：区域、品类、目标取自员工配置自带的主数据。"""
    adm = cfg["admission"]
    df, info = D.load_sales_many([A / "销售明细.csv"], adm["column_aliases"], adm["required_columns"], cfg["data"],
                                 D.read_master(EMP / "master.xlsx"))
    clean, stats = D.clean_sales(df)
    return clean, info, stats


def test_csv_joins_master_data(loaded):
    _, info, _ = loaded
    assert info["missing_columns"] == []
    assert info["lookups"]["joined"] == ["门店信息（按门店编码补充区域、店型、开业日期）", "商品信息（按商品编码补充品类）"]
    assert {"week": "W44", "region": "华东", "target": 235000.0} in info["targets"]
    assert info["date_range"] == ["2026-08-31", "2026-11-01"]


def test_without_master_data_columns_are_reported_missing(cfg):
    adm = cfg["admission"]
    _, info = D.load_sales_many([A / "销售明细.csv"], adm["column_aliases"], adm["required_columns"], cfg["data"], {})
    assert set(info["missing_columns"]) == {"区域", "品类"}
    assert info["lookups"]["missing_sheets"] == ["门店信息", "商品信息"]


def test_duplicates_by_record_id_and_returns_kept(loaded):
    _, _, stats = loaded
    assert stats["duplicates"] == 29
    dup = [i for i in stats["issues"] if i["kind"] == "duplicate"]
    assert len(dup) == 1 and dup[0]["date"] == "2026-10-21" and dup[0]["count"] == 29
    assert round(dup[0]["amount"]) == 5629
    assert stats["negative_rows"] == 0 and stats["return_rows"] == 124  # 退货保留，按负数冲减


def _kpis(loaded, week):
    df, info, _ = loaded
    return D.compute_kpis(df, parse_week(week), 0.10, 20, 0, ["region", "store"], info["targets"])


def test_w44_core_metrics(loaded):
    k = _kpis(loaded, "2026-W44")
    f = k["flat"]
    assert round(f["total.sales"]) == 573635
    assert round(f["total.sales_wow"] * 100, 1) == -9.3
    assert round(f["total.gross_margin"] * 100, 1) == 61.1
    assert round(f["total.delivery_share"] * 100, 1) == 31.8
    assert {g: round(f[f"region.{g}.target_rate"] * 100, 1) for g in ("华东", "华南", "华北", "西南")} == \
        {"华东": 84.1, "华南": 92.3, "华北": 92.1, "西南": 96.3}
    assert k["top_items"] == ["拿铁", "生椰拿铁", "美式咖啡", "6寸生日蛋糕", "草莓奶油切块"]
    assert k["quality"]["returns"]["count"] == 11 and round(k["quality"]["returns"]["amount"]) == -260
    assert [g["date"] for g in k["quality"]["missing_days"] if g["store"] == "杭州湖滨店"] == \
        ["2026-10-27", "2026-10-28", "2026-10-29"]
    a = {x["label"]: [d["label"] for d in x["drivers"]] for x in k["anomalies"]}
    assert a == {"华东区": ["上海静安店（SH02）", "杭州湖滨店（HZ01）"], "华南区": ["深圳南山店（SZ01）", "深圳福田店（SZ02）"]}


def test_w43_margin_shift_and_previous_week_gap(loaded):
    k = _kpis(loaded, "2026-W43")
    shift = k["margin_shifts"]
    assert [(s["region"], s["category"]) for s in shift] == [("华南", "饮品")]
    assert round(shift[0]["prev"] * 100, 1) == 70.4 and round(shift[0]["cur"] * 100, 1) == 60.5
    assert k["quality"]["missing_days_prev"] == [{"store": "广州海珠店", "region": "华南", "date": "2026-10-14"}]


def test_w42_store_anomalies_grouped_by_store_type(loaded):
    k = _kpis(loaded, "2026-W42")
    labels = [a["label"] for a in k["anomalies"]]
    assert "写字楼店（3 家）" in labels and "商场店（2 家）" in labels
    office = next(a for a in k["anomalies"] if a["label"] == "写字楼店（3 家）")
    assert {d["store"] for d in office["drivers"]} == {"北京海淀店", "深圳福田店", "成都高新店"}
    assert k["quality"]["missing_days"] == [{"store": "广州海珠店", "region": "华南", "date": "2026-10-14"}]
    assert round(k["quality"]["returns"]["amount"]) == -656


def test_minutes_headings_speakers_and_action_tables():
    items = D.read_minutes(A / "周会纪要_2026W44.docx")
    assert D.minutes_meta(A / "周会纪要_2026W44.docx")["week"] == 44
    assert not any("会议时间" in m["text"] or "参会人员" in m["text"] for m in items)
    south = next(m for m in items if m["ref"] == "二、2")
    assert south["header"] == "华南（陈某）" and "基本持平" in south["text"] and south["week"] == 44
    actions = [m for m in items if m["section"] == "五、行动项"]
    assert [m["text"] for m in actions][0] == "静安店会员日活动方案（负责人：李某，截止日期：11/03）"


def test_flat_claim_is_checked_against_data():
    c = [x for x in claims("陈某表示，本周华南整体销售与上周基本持平，活动结束后客流稳定") if x["kind"] == "flat"]
    assert len(c) == 1 and c[0]["text"] == "基本持平"
    assert check(c[0], -12.7) == "mismatch" and check(c[0], 1.5) == "consistent"
    assert not claims("华北（赵某）：本周平稳，北京海淀店写字楼客群稳定")  # 没说销售，不核对


def test_page_estimate_counts_overflow():
    from mini_employee.render import count_pages
    blocks = [("title", "经营周报"), ("h2", "一、本周摘要")] + [("p", "中文段落内容" * 30)] * 30
    assert count_pages(blocks) >= 3
    assert count_pages([("title", "经营周报"), ("p", "一句话")]) == 1


def test_driver_store_codes_added_on_first_mention():
    from mini_employee.render import _label_drivers
    item = {"phenomenon": "上海静安店环比下降 17.9%，杭州湖滨店（HZ01）环比下降 38.3%。", "cause": "上海静安店对面竞品开业。",
            "suggestion": ""}
    _label_drivers(item, [{"store": "上海静安店", "label": "上海静安店（SH02）"},
                          {"store": "杭州湖滨店", "label": "杭州湖滨店（HZ01）"}])
    assert item["phenomenon"] == "上海静安店（SH02）环比下降 17.9%，杭州湖滨店（HZ01）环比下降 38.3%。"
    assert item["cause"] == "上海静安店对面竞品开业。"  # 只补第一次出现


def test_signed_value_after_word_with_gap():
    from mini_employee.qc.rules import SIGNED_AFTER_WORD
    m = SIGNED_AFTER_WORD.search("两店合计拉低华东区本周实收 {{anomaly.A1.impact}}")
    assert m and m.group(3) == "anomaly.A1.impact"
    assert not SIGNED_AFTER_WORD.search("环比下降，变动 {{anomaly.A1.impact}}")
    assert not SIGNED_AFTER_WORD.search("环比下降 {{store.a.sales_wow|abs}}")


def test_missing_driver_stores_appended_by_program():
    from mini_employee.render import _label_drivers
    item = {"phenomenon": "华东五店同步下滑，主要受上海静安店拖累", "cause": "节后客流回落。", "suggestion": ""}
    _label_drivers(item, [{"store": "上海静安店", "label": "上海静安店（SH02）", "wow": -0.177},
                          {"store": "上海徐汇店", "label": "上海徐汇店（SH01）", "wow": -0.198}])
    assert item["phenomenon"] == "华东五店同步下滑，主要受上海静安店（SH02）拖累。带动门店还有：上海徐汇店（SH01）环比 -19.8%。"
