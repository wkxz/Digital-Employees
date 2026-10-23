"""生成场景 A 的模拟数据（企业正式样例下发前使用，全部为虚构数据，不含个人信息）。

输出：
  data/samples/   主样例：2026-W40~W44 门店销售明细 + 第 44 周周会纪要
  data/fixtures/  用于自测用例的变体：别名列、缺上周、脏数据、隐私、缺列、无异常周、跨年周等

W44 预埋的业务事件（纪要中有对应说明的打 ✓）：
  ✓ 上海徐汇店 10-27~10-29 装修闭店      → 华东区下滑
  ✓ 上海浦东店 周边竞品开业，客流下降     → 华东区下滑
  ✓ 华南区“生鲜周”促销                    → 华南·生鲜 大涨
  ✓ 北京海淀店 周边新小区入住             → 门店大涨
  ✗ 西南区 饮料 下滑                       → 纪要未提及，报告应写“原因待确认”
"""
from __future__ import annotations

import random
import sys
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
from docx import Document

ROOT = Path(__file__).resolve().parents[1]
SAMPLES = ROOT / "data" / "samples"
FIXTURES = ROOT / "data" / "fixtures"

STORES = [
    ("S01", "上海徐汇店", "华东", "上海", 1.30),
    ("S02", "上海浦东店", "华东", "上海", 1.20),
    ("S03", "杭州西湖店", "华东", "杭州", 1.00),
    ("S04", "南京新街口店", "华东", "南京", 0.95),
    ("S05", "广州天河店", "华南", "广州", 1.15),
    ("S06", "深圳南山店", "华南", "深圳", 1.10),
    ("S07", "深圳福田店", "华南", "深圳", 0.90),
    ("S08", "北京朝阳店", "华北", "北京", 1.20),
    ("S09", "北京海淀店", "华北", "北京", 1.00),
    ("S10", "天津和平店", "华北", "天津", 0.80),
    ("S11", "成都锦江店", "西南", "成都", 1.00),
    ("S12", "重庆解放碑店", "西南", "重庆", 0.90),
]
# 品类：日均基准销售额（元）、件均价
CATEGORIES = {"生鲜": (9000, 18), "饮料": (6000, 6), "零食": (5000, 12), "日化": (4000, 25), "粮油": (3500, 30)}
WEEKDAY = [1.0, 0.95, 0.97, 1.0, 1.1, 1.3, 1.25]
BASKET = 85.0


def week_days(year: int, week: int) -> list[date]:
    start = date.fromisocalendar(year, week, 1)
    return [start + timedelta(days=i) for i in range(7)]


def shock_w44(store, region, cat, d: date) -> float:
    """第 44 周预埋事件，返回乘数；返回 0 表示当天闭店（不产生记录）。"""
    sid = store[0]
    if sid == "S01" and date(2026, 10, 27) <= d <= date(2026, 10, 29):
        return 0.0
    f = 1.0
    if sid == "S02":
        f *= 0.72
    if region == "华南" and cat == "生鲜":
        f *= 1.32
    if sid == "S09":
        f *= 1.22
    if region == "西南" and cat == "饮料":
        f *= 0.78
    if cat == "日化":
        f *= 0.95
    return f


def gen_sales(weeks: list[tuple[int, int]], seed: int = 44, shocks: dict | None = None) -> pd.DataFrame:
    """shocks: {(year, week): fn(store, region, cat, day) -> factor}"""
    rng = random.Random(seed)
    shocks = shocks or {}
    rows = []
    for y, w in weeks:
        for d in week_days(y, w):
            holiday = 1.35 if date(2026, 10, 1) <= d <= date(2026, 10, 7) else 1.0
            for store in STORES:
                sid, name, region, city, size = store
                for cat, (base, price) in CATEGORIES.items():
                    f = shocks[(y, w)](store, region, cat, d) if (y, w) in shocks else 1.0
                    if f == 0:
                        continue
                    sales = base * size * WEEKDAY[d.weekday()] * holiday * f * rng.gauss(1, 0.05)
                    qty = max(1, round(sales / price * rng.gauss(1, 0.03)))
                    orders = max(1, round(sales / BASKET * rng.gauss(1, 0.04)))
                    rows.append({
                        "日期": d.isoformat(), "门店编号": sid, "门店": name, "区域": region, "城市": city,
                        "品类": cat, "销售额": round(sales, 2), "销量": qty, "订单数": orders,
                    })
    return pd.DataFrame(rows)


def write_minutes(path: Path, title: str, items: list[str]) -> None:
    if path.suffix == ".md":
        lines = [f"# {title}", ""] + [f"{i}. {t}" for i, t in enumerate(items, 1)]
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return
    doc = Document()
    doc.add_heading(title, level=1)
    for i, t in enumerate(items, 1):
        doc.add_paragraph(f"{i}. {t}")
    doc.save(path)


MINUTES_W44 = [
    "会议时间：2026 年 11 月 2 日；参会：运营总监、各区域经理、供应链负责人、财务 BP。",
    "华东区：上海徐汇店 10 月 27 日至 29 日进行门头和冷柜改造，闭店 3 天，30 日恢复营业。",
    "华东区：上海浦东店周边 10 月 26 日新开一家竞品社区超市，开业期间满 100 减 30，本店客流明显下降。",
    "华南区：本周开展“生鲜周”促销活动，生鲜品类全场 8.8 折，配合社群推送，客流和客单均有提升。",
    "华北区：北京海淀店周边新交付小区陆续入住，新增会员较多，门店整体销售上升。",
    "供应链：日化品类部分洗护 SKU 因供应商排产问题缺货，预计下周三前补齐。",
    "下周计划：华东区针对竞品开展会员日活动；华南区评估生鲜周效果后决定是否常态化；日化补货完成后复盘。",
]

MINUTES_CALM = [
    "会议时间：2026 年 11 月 2 日；参会：运营总监、各区域经理。",
    "各区域本周经营平稳，无重大活动和门店异常。",
    "下周计划：继续推进会员拉新，关注冬季品类备货。",
]

MINUTES_2027W01 = [
    "会议时间：2027 年 1 月 11 日；参会：运营总监、各区域经理。",
    "元旦假期结束后客流回落属季节性正常现象，各区域按计划推进年货节备货。",
    "下周计划：启动年货节预热。",
]


def calm_shock(store, region, cat, d):
    return 1.0


def main() -> None:
    SAMPLES.mkdir(parents=True, exist_ok=True)
    FIXTURES.mkdir(parents=True, exist_ok=True)

    weeks = [(2026, w) for w in range(40, 45)]
    df = gen_sales(weeks, shocks={(2026, 44): shock_w44})
    df.to_excel(SAMPLES / "销售明细_2026W44.xlsx", index=False)
    write_minutes(SAMPLES / "周会纪要_2026W44.docx", "第 44 周经营周会纪要", MINUTES_W44)

    # —— 变体 ——
    df.to_csv(FIXTURES / "销售明细_2026W44.csv", index=False, encoding="utf-8-sig")
    write_minutes(FIXTURES / "周会纪要_2026W44.md", "第 44 周经营周会纪要", MINUTES_W44)

    alias = df.rename(columns={"日期": "销售日期", "门店": "门店名称", "区域": "大区", "品类": "类别", "销售额": "金额", "销量": "数量"})
    alias.to_excel(FIXTURES / "销售明细_别名列.xlsx", index=False)

    df[df["日期"] >= "2026-10-26"].to_excel(FIXTURES / "销售明细_仅本周.xlsx", index=False)
    df[df["日期"] <= "2026-10-29"].to_excel(FIXTURES / "销售明细_本周不完整.xlsx", index=False)
    df.drop(columns=["品类"]).to_excel(FIXTURES / "销售明细_缺品类列.xlsx", index=False)

    rng = random.Random(7)

    def dirty(ratio: float) -> pd.DataFrame:
        d = df.copy()
        n = int(len(d) * ratio / 3)
        dup = d.sample(n, random_state=1)
        idx_neg = rng.sample(list(d.index), n)
        d.loc[idx_neg, "销售额"] = -d.loc[idx_neg, "销售额"]
        idx_null = rng.sample(list(d.index), n)
        d.loc[idx_null, "品类"] = None
        return pd.concat([d, dup]).sort_values(["日期", "门店编号"]).reset_index(drop=True)

    dirty(0.03).to_excel(FIXTURES / "销售明细_少量脏数据.xlsx", index=False)
    dirty(0.12).to_excel(FIXTURES / "销售明细_大量脏数据.xlsx", index=False)

    pii = df.copy()
    pii["会员手机号"] = ""
    for i in rng.sample(list(pii.index), 30):
        pii.loc[i, "会员手机号"] = f"13{rng.randint(100000000, 999999999)}"
    pii.to_excel(FIXTURES / "销售明细_含手机号.xlsx", index=False)

    calm = gen_sales(weeks, seed=45, shocks={(2026, 44): calm_shock})
    calm.to_excel(FIXTURES / "销售明细_平稳周.xlsx", index=False)
    write_minutes(FIXTURES / "周会纪要_平稳周.docx", "第 44 周经营周会纪要", MINUTES_CALM)

    xyear = gen_sales([(2026, 51), (2026, 52), (2026, 53), (2027, 1)], seed=53)
    xyear.to_excel(FIXTURES / "销售明细_2027W01.xlsx", index=False)
    write_minutes(FIXTURES / "周会纪要_2027W01.docx", "2027 年第 1 周经营周会纪要", MINUTES_2027W01)

    write_minutes(FIXTURES / "周会纪要_空白.docx", "第 44 周经营周会纪要", [])
    (FIXTURES / "经营数据.pdf").write_bytes(b"%PDF-1.4\n% not a real report\n")

    print(f"samples: {sorted(p.name for p in SAMPLES.iterdir())}")
    print(f"fixtures: {len(list(FIXTURES.iterdir()))} files")


if __name__ == "__main__":
    sys.exit(main())
