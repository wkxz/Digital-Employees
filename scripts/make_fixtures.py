"""从官方数据（data/official_a）生成用例用的变体数据，写到 data/fixtures/。

每个变体只改一处，便于用例定位问题：平稳周、列名别名、跨年周、少量 / 大量脏数据、含手机号、
只有本周、缺品类、本周不完整、Markdown 纪要、空白纪要、只含销售目标的主数据。
运行：uv run python scripts/make_fixtures.py
"""
from __future__ import annotations

import datetime as dt
import shutil
from pathlib import Path

import docx
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "data" / "official_a"
OUT = ROOT / "data" / "fixtures"
SHEET = "销售明细"
rng = np.random.default_rng(20261027)


def book() -> dict[str, pd.DataFrame]:
    b = pd.read_excel(SRC / "销售明细.xlsx", sheet_name=None, dtype={"门店编码": str, "商品编码": str})
    b[SHEET]["销售日期"] = pd.to_datetime(b[SHEET]["销售日期"])
    return b


def save(b: dict[str, pd.DataFrame], name: str) -> None:
    with pd.ExcelWriter(OUT / name) as w:
        for k, v in b.items():
            v = v.copy()
            for c in v.columns:
                if pd.api.types.is_datetime64_any_dtype(v[c]):
                    v[c] = v[c].dt.date
            v.to_excel(w, sheet_name=k, index=False)


def week_rows(df: pd.DataFrame, start: str, end: str) -> pd.Series:
    d = df["销售日期"]
    return (d >= pd.Timestamp(start)) & (d <= pd.Timestamp(end))


W44 = ("2026-10-26", "2026-11-01")
W43 = ("2026-10-19", "2026-10-25")


def flat_week() -> None:
    """平稳周：第 44 周的明细换成第 43 周（去重后）整体后移 7 天，各区域、门店环比均为 0。"""
    b = book()
    df = b[SHEET]
    w43 = df[week_rows(df, *W43)].drop_duplicates("记录ID").copy()
    w43["销售日期"] += pd.Timedelta(days=7)
    w43["记录ID"] = "F" + w43["记录ID"].str[1:]
    b[SHEET] = pd.concat([df[~week_rows(df, *W44)], w43], ignore_index=True)
    save(b, "销售明细_平稳周.xlsx")


def alias_columns() -> None:
    """列名全部换成别名（见 employee.yaml 的 column_aliases）。"""
    b = book()
    b[SHEET] = b[SHEET].rename(columns={"销售日期": "交易日期", "门店名称": "店名", "销售数量": "件数",
                                        "实收金额": "营业额", "成本金额": "成本", "商品名称": "单品",
                                        "记录ID": "流水号", "渠道": "销售渠道"})
    save(b, "销售明细_别名列.xlsx")


def cross_year() -> None:
    """跨年周：全部日期后移 10 周，2026-W44 → 2027-W01、2026-W43 → 2026-W53；销售目标的周次同步后移。"""
    b = book()
    shift = pd.Timedelta(weeks=10)
    b[SHEET]["销售日期"] += shift
    b[SHEET]["记录ID"] = "Y" + b[SHEET]["记录ID"].str[1:]
    t = b["销售目标"]
    start = pd.to_datetime(t["周起始日"]) + shift
    iso = start.dt.isocalendar()
    t["年份"], t["周次"] = iso["year"].astype(int), [f"W{w:02d}" for w in iso["week"]]
    t["周起始日"], t["周结束日"] = start.dt.date, (start + pd.Timedelta(days=6)).dt.date
    save(b, "销售明细_2027W01.xlsx")
    d = docx.Document(SRC / "周会纪要_2026W44.docx")
    p = d.paragraphs[0]
    for r in p.runs:
        r.text = r.text.replace("2026 年第 44 周", "2027 年第 1 周").replace("2026年第44周", "2027年第1周")
    assert "2027" in p.text, p.text
    d.save(OUT / "周会纪要_2027W01.docx")


def dirty(name: str, n_null: int, n_neg: int) -> None:
    """脏数据：第 44 周随机若干行实收为空、若干行销量为正而实收为负（不是退货）。"""
    b = book()
    df = b[SHEET]
    idx = rng.choice(np.flatnonzero(week_rows(df, *W44) & (df["销售数量"] > 0)), n_null + n_neg, replace=False)
    df.loc[idx[:n_null], "实收金额"] = np.nan
    df.loc[idx[n_null:], "实收金额"] = -df.loc[idx[n_null:], "实收金额"].abs()
    save(b, name)


def with_phone() -> None:
    """含会员手机号列：准入应按 pii_policy 拒绝。"""
    b = book()
    df = b[SHEET]
    df.insert(4, "会员手机号", [f"138{rng.integers(10_000_000, 99_999_999)}" for _ in range(len(df))])
    save(b, "销售明细_含手机号.xlsx")


def only_week44() -> None:
    b = book()
    b[SHEET] = b[SHEET][week_rows(b[SHEET], *W44)]
    save(b, "销售明细_仅本周.xlsx")


def no_category() -> None:
    """缺品类：明细去掉商品编码，无法按商品信息关联品类。"""
    b = book()
    b[SHEET] = b[SHEET].drop(columns=["商品编码"])
    save(b, "销售明细_缺品类列.xlsx")


def week_incomplete() -> None:
    """本周不完整：缺 10/30–11/01 三天。"""
    b = book()
    df = b[SHEET]
    b[SHEET] = df[~week_rows(df, "2026-10-30", "2026-11-01")]
    save(b, "销售明细_本周不完整.xlsx")


def minutes_md() -> None:
    """第 44 周纪要的 Markdown 版：一级标题 + “## 一、…”章节 + 正文，行动项表格转为 Markdown 表格。"""
    d = docx.Document(SRC / "周会纪要_2026W44.docx")
    out = []
    for i, p in enumerate(d.paragraphs):
        t = p.text.strip()
        if not t:
            continue
        if i == 0:
            out.append(f"# {t}")
        elif t[:2] in ("一、", "二、", "三、", "四、", "五、", "六、"):
            out.append(f"\n## {t}\n")
        else:
            out.append(t)
    for tb in d.tables:
        rows = [[c.text.strip() for c in r.cells] for r in tb.rows]
        out.append("")
        out.append("| " + " | ".join(rows[0]) + " |")
        out.append("|" + "---|" * len(rows[0]))
        out += ["| " + " | ".join(r) + " |" for r in rows[1:]]
    (OUT / "周会纪要_2026W44.md").write_text("\n".join(out) + "\n", encoding="utf-8")


def blank_minutes() -> None:
    d = docx.Document()
    d.add_paragraph("")
    d.save(OUT / "周会纪要_空白.docx")


def target_master() -> None:
    """只含“销售目标”工作表的主数据：华东第 44 周目标由 235,000 调为 205,000 元，其余不变。"""
    t = book()["销售目标"]
    t.loc[(t["区域"] == "华东") & (t["周次"] == "W44"), "实收目标(元)"] = 205000
    save({"销售目标": t}, "主数据_目标调整.xlsx")


def pdf() -> None:
    """伪装成销售明细的 PDF（内容无关紧要，用于表单校验拒绝）。"""
    (OUT / "经营数据.pdf").write_bytes(b"%PDF-1.4\n% fixture: not a sales table\n%%EOF\n")


def main() -> None:
    if OUT.exists():
        shutil.rmtree(OUT)
    OUT.mkdir(parents=True)
    flat_week()
    alias_columns()
    cross_year()
    dirty("销售明细_少量脏数据.xlsx", n_null=40, n_neg=20)   # 约 0.2%，加上 29 条重复仍远低于 5%
    dirty("销售明细_大量脏数据.xlsx", n_null=2000, n_neg=600)  # 约 6%，超过 5% 暂停
    with_phone()
    only_week44()
    no_category()
    week_incomplete()
    minutes_md()
    blank_minutes()
    target_master()
    pdf()
    shutil.copy(SRC / "销售明细.csv", OUT / "销售明细_2026W44.csv")
    print("written:", sorted(p.name for p in OUT.iterdir()), dt.datetime.now().isoformat(timespec="seconds"))


if __name__ == "__main__":
    main()
