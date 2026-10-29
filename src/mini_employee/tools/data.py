"""数据读取、清洗与 KPI 计算。所有数字在这里由程序算出，模型只能引用。"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pandas as pd
from docx import Document

from ..periods import Week

PHONE_RE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
IDCARD_RE = re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)")


class DataError(Exception):
    def __init__(self, kind: str, message: str, details: dict | None = None):
        super().__init__(message)
        self.kind, self.message, self.details = kind, message, details or {}


# ---------------------------------------------------------------- 销售数据

def read_book(path: Path, sheet: str | None = None) -> tuple[pd.DataFrame, dict[str, pd.DataFrame]]:
    """读取销售数据文件，返回 (明细表, 其他工作表)。xlsx 有名为 sheet 的工作表时读它，否则读第一个工作表；
    其他工作表（门店信息、商品信息、销售目标等主数据）一并返回，供关联使用。"""
    ext = path.suffix.lower()
    if ext in (".xlsx", ".xls"):
        book = pd.read_excel(path, sheet_name=None, engine="calamine")
        name = sheet if sheet in book else next(iter(book))
        return book[name], {k: v for k, v in book.items() if k != name}
    if ext == ".csv":
        for enc in ("utf-8-sig", "gbk"):
            try:
                return pd.read_csv(path, encoding=enc), {}
            except UnicodeDecodeError:
                continue
    raise DataError("INVALID_INPUT", f"不支持的销售数据格式：{path.suffix}（支持 xlsx / csv）")


def read_table(path: Path) -> pd.DataFrame:
    return read_book(path)[0]


def read_master(path: Path | None) -> dict[str, pd.DataFrame]:
    """主数据工作簿（门店信息、商品信息、销售目标）：发起人上传的，或员工配置自带的。"""
    if path is None or not path.exists():
        return {}
    return pd.read_excel(path, sheet_name=None, engine="calamine")


def apply_lookups(df: pd.DataFrame, lookups: list[dict[str, Any]], sources: dict[str, pd.DataFrame]
                  ) -> tuple[pd.DataFrame, dict[str, Any]]:
    """按编码从主数据表补齐明细中没有的列（如 门店编码 → 区域、开业日期；商品编码 → 品类）。
    明细里已有这些列（旧格式数据）时不关联。"""
    info: dict[str, Any] = {"joined": [], "missing_sheets": [], "unmatched": {}}
    for lk in lookups:
        key, fields = lk["key"], [f for f in lk["fields"] if f not in df.columns]
        if key not in df.columns or not fields:
            continue
        table = sources.get(lk["sheet"])
        if table is None or key not in table.columns:
            info["missing_sheets"].append(lk["sheet"])
            continue
        right = table[[key] + [f for f in fields if f in table.columns]].drop_duplicates(key)
        right[key] = right[key].astype(str).str.strip()
        df = df.assign(**{key: df[key].astype(str).str.strip()}).merge(right, on=key, how="left")
        bad = sorted(df.loc[df[fields[0]].isna(), key].dropna().unique().tolist())
        if bad:
            info["unmatched"][lk["sheet"]] = bad[:10]
        info["joined"].append(f"{lk['sheet']}（按{key}补充{'、'.join(fields)}）")
    return df, info


def read_targets(cfg: dict[str, Any] | None, sources: dict[str, pd.DataFrame]) -> list[dict[str, Any]]:
    """销售目标表 → [{week: "W44", region, target}]。"""
    if not cfg or cfg.get("sheet") not in sources:
        return []
    t = sources[cfg["sheet"]]
    cols = (cfg.get("week", "周次"), cfg.get("region", "区域"), cfg.get("value", "实收目标(元)"))
    if not all(c in t.columns for c in cols):
        return []
    return [{"week": str(w).strip().upper(), "region": str(r).strip(), "target": float(v)}
            for w, r, v in t[list(cols)].dropna().itertuples(index=False)]


def normalize_columns(df: pd.DataFrame, aliases: dict[str, list[str]]) -> tuple[pd.DataFrame, dict[str, str]]:
    """按配置的别名把列名统一成标准名，返回 (df, {原列名: 标准名})。"""
    mapping: dict[str, str] = {}
    cols = {str(c).strip(): c for c in df.columns}
    for std, alts in aliases.items():
        if std in cols:
            continue
        for a in alts:
            if a in cols:
                mapping[cols[a]] = std
                break
    return df.rename(columns=mapping), {str(k): v for k, v in mapping.items()}


def mask_pii(s: str) -> str:
    """手机号保留前 3 后 4 位，身份证号保留前 4 后 4 位。"""
    s = IDCARD_RE.sub(lambda m: m.group(0)[:4] + "*" * 10 + m.group(0)[-4:], s)
    return PHONE_RE.sub(lambda m: m.group(0)[:3] + "****" + m.group(0)[-4:], s)


def redact_columns(df: pd.DataFrame, info: dict[str, Any], required: list[str]) -> tuple[pd.DataFrame, list[str]]:
    """pii_policy=redact 时使用：含隐私的列，非必需的整列剔除，必需列（如门店名里混入手机号）逐值打码。"""
    done = []
    for raw in info.get("pii_columns", []):
        col = info.get("alias_mapping", {}).get(raw, raw)
        if col not in df.columns:
            continue
        if col in required:
            df[col] = df[col].astype(str).map(mask_pii)
        else:
            df = df.drop(columns=[col])
        done.append(raw)
    return df, done


def find_pii(df: pd.DataFrame) -> list[str]:
    """检测手机号 / 身份证号。Excel 常把手机号存成数字，因此数值列也转成整数文本检查。"""
    hits = []
    for c in df.columns:
        s = df[c].dropna()
        if pd.api.types.is_numeric_dtype(s):
            s = s[(s == s.round()) & (s.abs() < 1e18)].astype("int64")
        s = s.astype(str)
        if s.str.contains(PHONE_RE).any() or s.str.contains(IDCARD_RE).any():
            hits.append(str(c))
    return hits


MASTER_ORIGIN = {"upload": "上传的主数据文件", "sales": "销售明细中的同名工作表", "builtin": "员工配置自带的主数据"}


def master_origins(data_cfg: dict[str, Any], layers: dict[str, dict[str, pd.DataFrame]]) -> dict[str, str]:
    """主数据各工作表实际取自哪里（layers 按优先级从高到低排列）。"""
    names = [lk["sheet"] for lk in data_cfg.get("lookups", [])] + \
            ([data_cfg["targets"]["sheet"]] if data_cfg.get("targets") else [])
    return {n: next((MASTER_ORIGIN[k] for k, s in layers.items() if n in s), "未提供") for n in names}


def load_sales(path: Path, aliases: dict[str, list[str]], required: list[str],
               data_cfg: dict[str, Any] | None = None, master: dict[str, pd.DataFrame] | None = None,
               upload: dict[str, pd.DataFrame] | None = None) -> tuple[pd.DataFrame, dict[str, Any]]:
    """master：员工配置自带的主数据；upload：发起人上传的主数据文件。按工作表取用，
    优先级：上传的主数据文件 > 销售明细 xlsx 中的同名工作表 > 员工配置自带的（门店新开、目标调整时上传新表即可）。"""
    data_cfg = data_cfg or {}
    raw, sheets = read_book(path, data_cfg.get("sheet"))
    df, mapping = normalize_columns(raw, aliases)
    sources = {**(master or {}), **sheets, **(upload or {})}
    df, joined = apply_lookups(df, data_cfg.get("lookups", []), sources)
    missing = [c for c in required if c not in df.columns]
    info: dict[str, Any] = {
        "rows": int(len(df)), "columns": [str(c) for c in raw.columns], "alias_mapping": mapping,
        "missing_columns": missing, "pii_columns": find_pii(raw), "lookups": joined,
        "targets": read_targets(data_cfg.get("targets"), sources),
        "master_origin": master_origins(data_cfg, {"upload": upload or {}, "sales": sheets, "builtin": master or {}}),
    }
    if not missing:
        dates = pd.to_datetime(df["日期"], errors="coerce").dropna()
        if len(dates):
            info["date_range"] = [dates.min().date().isoformat(), dates.max().date().isoformat()]
            iso = dates.dt.isocalendar()
            info["weeks"] = sorted({f"{y}-W{w:02d}" for y, w in zip(iso["year"], iso["week"])})
    return df, info


KEY = ["日期", "门店", "品类"]


def load_sales_many(paths: list[Path], aliases: dict[str, list[str]], required: list[str],
                    data_cfg: dict[str, Any] | None = None, master: dict[str, pd.DataFrame] | None = None,
                    upload: dict[str, pd.DataFrame] | None = None) -> tuple[pd.DataFrame, dict[str, Any]]:
    """读取并合并多份销售明细（如先传本周、再补传上周）。各文件分别识别列名；
    同一 日期+门店+品类 在多份文件中都出现时，以后上传的文件为准（补传更正版即可覆盖）。"""
    loaded = []
    for i, p in enumerate(paths):
        df, info = load_sales(p, aliases, required, data_cfg, master, upload)
        df["_src"] = i
        dates = set(pd.to_datetime(df["日期"], errors="coerce").dropna().dt.date) if "日期" in df.columns else set()
        loaded.append((p, df, info, dates))
    # 更正版重传：后上传的文件列齐全时，丢弃先前缺列的文件；后上传的文件覆盖了先前文件的全部日期时，先前文件整体作废
    # 按上传顺序（下标）判断，不按文件名：更正版常与原文件同名，按名字会把更正版也一起作废
    superseded_idx = []
    for i, (p, _, info, dates) in enumerate(loaded):
        later = [x for x in loaded[i + 1:] if not x[2]["missing_columns"]]
        if later and (info["missing_columns"] or any(dates and dates <= x[3] for x in later)):
            superseded_idx.append(i)
    kept_idx = [i for i in range(len(loaded)) if i not in superseded_idx] or [len(loaded) - 1]
    superseded_idx = [i for i in range(len(loaded)) if i not in kept_idx]
    superseded = [loaded[i][0].name.split("_", 1)[-1] for i in superseded_idx]
    kept = [loaded[i] for i in kept_idx]
    paths = [x[0] for x in kept]
    frames, infos = [x[1] for x in kept], [x[2] for x in kept]
    missing = sorted({c for inf in infos for c in inf["missing_columns"]})
    df = pd.concat(frames, ignore_index=True)
    overridden = 0
    if len(paths) > 1 and not missing:
        cols = ["记录ID"] if "记录ID" in df.columns else KEY + [c for c in ("商品", "渠道") if c in df.columns]
        key = df[cols].assign(**({"日期": pd.to_datetime(df["日期"], errors="coerce")} if "日期" in cols else {}))
        latest = df.groupby([key[c] for c in cols], dropna=False)["_src"].transform("max")
        stale = df["_src"] < latest
        overridden = int(stale.sum())
        df = df[~stale]
    info: dict[str, Any] = {
        "rows": int(len(df)), "columns": list(dict.fromkeys(c for inf in infos for c in inf["columns"])),
        "alias_mapping": {k: v for inf in infos for k, v in inf["alias_mapping"].items()},
        "missing_columns": missing, "pii_columns": sorted({c for inf in infos for c in inf["pii_columns"]}),
        "files": [{"name": p.name.split("_", 1)[-1], "rows": inf["rows"], "date_range": inf.get("date_range")}
                  for p, inf in zip(paths, infos)],
        "overridden_rows": overridden, "superseded_files": superseded,
        "superseded_index": superseded_idx,
        "lookups": {k: v for inf in infos for k, v in inf["lookups"].items() if k != "joined"}
        | {"joined": list(dict.fromkeys(j for inf in infos for j in inf["lookups"]["joined"]))},
        "targets": next((inf["targets"] for inf in reversed(infos) if inf["targets"]), []),
        "master_origin": infos[-1]["master_origin"],  # 上传的主数据对每份明细都一样；明细自带的工作表以后上传的为准
    }
    if not missing:
        dates = pd.to_datetime(df["日期"], errors="coerce").dropna()
        if len(dates):
            info["date_range"] = [dates.min().date().isoformat(), dates.max().date().isoformat()]
            iso = dates.dt.isocalendar()
            info["weeks"] = sorted({f"{y}-W{w:02d}" for y, w in zip(iso["year"], iso["week"])})
    return df.drop(columns="_src"), info


def _row_desc(r: pd.Series) -> str:
    d = r["日期"].strftime("%m-%d") if pd.notna(r["日期"]) else "日期缺失"
    what = r.get("商品") if pd.notna(r.get("商品", None)) else r.get("品类", "")
    return f"{d} {r.get('门店', '')} {what if pd.notna(what) else ''}".strip()


def _iso(d: Any) -> str | None:
    return d.date().isoformat() if pd.notna(d) else None


def _dedupe(df: pd.DataFrame, issues: list[dict[str, Any]]) -> tuple[pd.DataFrame, int]:
    """有记录ID时按记录ID去重（重复上传），按门店、日期汇总成一条说明；没有时按整行完全相同去重。"""
    if "记录ID" not in df.columns:
        dup = df.duplicated()
        for _, r in df[dup].iterrows():
            issues.append({"row": _row_desc(r), "date": _iso(r["日期"]), "kind": "duplicate",
                           "problem": "与另一行完全相同", "action": "删除重复行"})
        return df[~dup], int(dup.sum())
    dup = df["记录ID"].notna() & df.duplicated("记录ID")
    same = df.duplicated()
    for (store, day), g in df[dup].groupby(["门店", "日期"], dropna=False):
        n_conf = int((~same[g.index]).sum())
        issues.append({
            "row": f"{day.strftime('%m-%d') if pd.notna(day) else '日期缺失'} {store}", "date": _iso(day),
            "kind": "duplicate", "count": int(len(g)), "amount": round(float(g["销售额"].sum()), 2),
            "problem": f"记录ID 重复 {len(g)} 条（实收合计 {g['销售额'].sum():,.0f} 元）"
                       + (f"，其中 {n_conf} 条与首条内容不同" if n_conf else "，与已有记录完全相同，属重复上传"),
            "action": "按记录ID去重，只保留首条" + ("，内容不同的请核实" if n_conf else "")})
    return df[~dup], int(dup.sum())


def clean_sales(df: pd.DataFrame) -> tuple[pd.DataFrame, dict[str, Any]]:
    """清洗规则（每一处处理都记入 issues，供“数据质量说明”和审核人逐条核对）：
    - 重复：有记录ID时按记录ID去重（重复上传），否则删除完全相同的行（销售额为 0 的停业记录不会被误判为重复）
    - 关键字段为空：整行剔除
    - 退货（销量、销售额均为负）：保留，按负数冲减当期实收
    - 销售额为负但不是退货（含 -9999 之类的占位值）：整行剔除
    - 销售额正常但销量为负：保留销售额，该行销量不计入
    - 销售额为 0：保留（多为停业），按门店汇总日期，便于与纪要核对
    """
    total = len(df)
    df = df.copy()
    df["日期"] = pd.to_datetime(df["日期"], errors="coerce")
    for c in ("销售额", "销量", "订单数", "成本"):
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    issues: list[dict[str, Any]] = []
    df, n_dup = _dedupe(df, issues)
    null_mask = df[["日期", "门店", "区域", "品类", "销售额"]].isna().any(axis=1)
    for _, r in df[null_mask].iterrows():
        cols = [c for c in ("日期", "门店", "区域", "品类", "销售额") if pd.isna(r[c])]
        issues.append({"row": _row_desc(r), "date": _iso(r["日期"]), "kind": "null",
                       "problem": f"{'、'.join(cols)}为空", "action": "整行剔除"})
    returns = (df["销售额"] < 0) & (df["销量"] < 0) & ~null_mask
    neg_sales = (df["销售额"] < 0) & ~null_mask & ~returns
    for _, r in df[neg_sales].iterrows():
        v = r["销售额"]
        hint = "，疑似占位值" if v in (-9999, -999, -99999, -1) else ""
        issues.append({"row": _row_desc(r), "date": _iso(r["日期"]), "kind": "negative",
                       "problem": f"销售额为 {v:g}{hint}", "action": "整行剔除"})
    df = df[~null_mask & ~neg_sales].copy()
    bad_qty = df["销量"].isna() | ((df["销量"] < 0) & (df["销售额"] >= 0))
    for _, r in df[bad_qty].iterrows():
        issues.append({"row": _row_desc(r), "date": _iso(r["日期"]), "kind": "qty",
                       "problem": f"销量为 {r['销量']:g}" if pd.notna(r["销量"]) else "销量为空",
                       "action": "保留销售额，该行销量不计入"})
    df.loc[bad_qty, "销量"] = float("nan")
    zero = df[df["销售额"] == 0]
    zero_days = {store: sorted(g["日期"].dt.strftime("%m-%d").unique()) for store, g in zero.groupby("门店")}
    dropped = int(n_dup + null_mask.sum() + neg_sales.sum())
    return df, {
        "total_rows": total, "duplicates": n_dup, "null_rows": int(null_mask.sum()),
        "negative_rows": int(neg_sales.sum()), "return_rows": int(returns.sum()), "bad_qty_rows": int(bad_qty.sum()),
        "zero_sales_rows": int(len(zero)), "zero_sales_days": zero_days, "kept_rows": int(len(df)),
        "bad_ratio": round((dropped + int(bad_qty.sum())) / total, 4) if total else 0.0, "issues": issues,
    }


# ---------------------------------------------------------------- KPI

DELIVERY = ("外卖",)
RATIOS = ("gross_margin", "delivery_share")  # 比率指标：不算相对环比，用“变动（百分点）”表示


def _agg(df: pd.DataFrame) -> dict[str, float]:
    sales = float(df["销售额"].sum())
    out = {"sales": sales, "qty": float(df["销量"].sum())}
    if "订单数" in df.columns:
        out["orders"] = float(df["订单数"].sum())
        out["atv"] = out["sales"] / out["orders"] if out["orders"] else 0.0
    if "成本" in df.columns:  # 毛利 = 实收 − 成本；毛利率 = 毛利 ÷ 实收
        out["gross_profit"] = sales - float(df["成本"].sum())
        out["gross_margin"] = out["gross_profit"] / sales if sales else 0.0
    if "渠道" in df.columns:
        out["delivery_sales"] = float(df.loc[df["渠道"].isin(DELIVERY), "销售额"].sum())
        out["delivery_share"] = out["delivery_sales"] / sales if sales else 0.0
    return out


def _with_wow(cur: dict[str, float], prev: dict[str, float]) -> dict[str, float]:
    out = dict(cur)
    for k, v in cur.items():
        p = prev.get(k, 0.0)
        out[f"{k}_prev"] = p
        if k in RATIOS:
            out[f"{k}_chg"] = v - p if k in prev else None
        else:
            out[f"{k}_wow"] = (v / p - 1) if p else None
    return out


def _target_of(targets: list[dict[str, Any]], week: Week) -> dict[str, float]:
    keys = {f"W{week.week}", f"W{week.week:02d}", week.key, week.compact}
    return {t["region"]: t["target"] for t in targets if t["week"] in keys}


def _store_days(df: pd.DataFrame, week: Week) -> list[dict[str, Any]]:
    """已开业门店在该周没有任何销售记录的日期（系统故障、停业等）。开业日取主数据的开业日期，没有时取首次有销售的日期。"""
    if df.empty:
        return []
    first = df.groupby("门店")["日期"].min()
    if "开业日期" in df.columns:
        opened = pd.to_datetime(df.groupby("门店")["开业日期"].first(), errors="coerce")
    else:
        opened = first
    have = set(zip(df["门店"], df["日期"].dt.normalize()))
    region = df.drop_duplicates("门店").set_index("门店")["区域"].to_dict()
    out = []
    for store in sorted(first.index):
        start = opened.get(store)
        start = first[store] if pd.isna(start) else start
        for d in week.days():
            ts = pd.Timestamp(d)
            if ts >= start and (store, ts) not in have:
                out.append({"store": store, "region": region.get(store), "date": d.isoformat()})
    return out


def _returns(cur: pd.DataFrame) -> dict[str, Any]:
    r = cur[(cur["销量"] < 0) & (cur["销售额"] < 0)]
    what = "商品" if "商品" in r.columns else "品类"
    top = (r.groupby(["门店", "日期", what], as_index=False)[["销量", "销售额"]].sum()
           .sort_values("销售额").head(3))
    return {"count": int(len(r)), "amount": float(r["销售额"].sum()),
            "top": [{"store": x["门店"], "date": x["日期"].date().isoformat(), "item": x[what],
                     "qty": float(-x["销量"]), "amount": float(x["销售额"])} for _, x in top.iterrows()]}


def compute_kpis(df: pd.DataFrame, week: Week, threshold: float, top_n: int, min_base: float,
                 dimensions: list[str], targets: list[dict[str, Any]] | None = None, top_items: int = 5,
                 margin_shift: float = 0.05) -> dict[str, Any]:
    cur = df[(df["日期"] >= pd.Timestamp(week.start)) & (df["日期"] <= pd.Timestamp(week.end))]
    pw = week.prev
    prev = df[(df["日期"] >= pd.Timestamp(pw.start)) & (df["日期"] <= pd.Timestamp(pw.end))]
    if cur.empty:
        raise DataError("MISSING_INPUT", f"销售数据中没有 {week.key} 的记录")
    if prev.empty:
        raise DataError("MISSING_INPUT", f"无法计算环比，请补充上一周（{pw.key}）的销售数据")
    have = {d.date() for d in cur["日期"].unique()}
    lacking = [d.isoformat() for d in week.days() if d not in have]
    if lacking:
        raise DataError("MISSING_INPUT", f"本周数据不完整，缺少 {', '.join(lacking)} 的销售记录，请补充后继续",
                        {"missing_dates": lacking})
    have_prev = {d.date() for d in prev["日期"].unique()}
    lacking_prev = [d.isoformat() for d in pw.days() if d not in have_prev]
    if lacking_prev:  # 上周缺天会让环比虚高、误报异常
        raise DataError("MISSING_INPUT", f"上一周（{pw.key}）数据不完整，缺少 {', '.join(lacking_prev)} 的销售记录，"
                                         "环比会失真，请补充后继续", {"missing_dates": lacking_prev})

    flat: dict[str, Any] = {}
    total = _with_wow(_agg(cur), _agg(prev))
    total["stores"] = int(cur["门店"].nunique())
    goal = _target_of(targets or [], week)

    groups = {"region": ["区域"], "store": ["门店"], "category": ["品类"], "rc": ["区域", "品类"]}
    if "商品" in df.columns:
        groups["item"] = ["商品"]
    tables: dict[str, list[dict]] = {}
    for dim, cols in groups.items():
        c_grp = {k if isinstance(k, tuple) else (k,): g for k, g in cur.groupby(cols)}
        p_grp = {k if isinstance(k, tuple) else (k,): g for k, g in prev.groupby(cols)}
        rows = []
        for key in sorted(set(c_grp) | set(p_grp)):
            c = _agg(c_grp[key]) if key in c_grp else {"sales": 0.0, "qty": 0.0}
            p = _agg(p_grp[key]) if key in p_grp else {"sales": 0.0, "qty": 0.0}
            m = _with_wow(c, p)
            m["share"] = m["sales"] / total["sales"] if total["sales"] else 0.0
            name = ".".join(key)
            if dim == "region" and name in goal:  # 目标达成率 = 本周实收 ÷ 该区域本周目标
                m["target"] = goal[name]
                m["target_rate"] = m["sales"] / goal[name] if goal[name] else None
            for k, v in m.items():
                if dim != "item" or k in ("sales", "sales_wow", "share"):
                    flat[f"{dim}.{name}.{k}"] = v
            rows.append({"name": name, "keys": list(key), **m})
        if dim == "store":
            info = cur.drop_duplicates("门店").set_index("门店")
            for r in rows:
                r["region"] = info["区域"].get(r["name"])
                for col, k in (("门店编码", "code"), ("店型", "store_type")):
                    if col in info.columns:
                        r[k] = info[col].get(r["name"])
        if dim == "item":
            cat = cur.drop_duplicates("商品").set_index("商品")["品类"].to_dict()
            for r in rows:
                r["category"] = cat.get(r["name"])
        tables[dim] = rows
    regions = {r["name"] for r in tables["region"]}
    if goal and regions <= set(goal):
        total["target"] = sum(goal[g] for g in regions)
        total["target_rate"] = total["sales"] / total["target"] if total["target"] else None
    for k, v in total.items():
        flat[f"total.{k}"] = v
    ranked = sorted(tables.get("item", []), key=lambda r: r["sales"], reverse=True)[:top_items]
    for i, r in enumerate(ranked, 1):
        r["rank"] = i

    daily = []
    for i, d in enumerate(week.days()):
        pd_ = pw.days()[i]
        daily.append({
            "date": d.isoformat(), "weekday": "一二三四五六日"[i],
            "sales": float(cur.loc[cur["日期"] == pd.Timestamp(d), "销售额"].sum()),
            "sales_prev": float(prev.loc[prev["日期"] == pd.Timestamp(pd_), "销售额"].sum()),
        })
    weekly = []  # 近 4 周实收趋势（只取数据完整的周）
    w = week
    for _ in range(4):
        part = df[(df["日期"] >= pd.Timestamp(w.start)) & (df["日期"] <= pd.Timestamp(w.end))]
        if part["日期"].dt.date.nunique() < 7:
            break
        weekly.insert(0, {"week": w.key, "label": f"W{w.week}", "sales": float(part["销售额"].sum()),
                          "target": sum(_target_of(targets or [], w).values()) or None})
        w = w.prev

    shifts = []  # 区域×品类毛利率明显变动（如促销让利），供分析与摘要引用
    for r in tables["rc"]:
        chg = r.get("gross_margin_chg")
        if chg is not None and r["sales_prev"] > 0 and abs(chg) >= margin_shift:
            shifts.append({"region": r["keys"][0], "category": r["keys"][1], "chg": chg,
                           "cur": r["gross_margin"], "prev": r["gross_margin_prev"],
                           "placeholder": f"rc.{r['name']}.gross_margin"})
    quality = {"returns": _returns(cur), "missing_days": _store_days(cur, week),
               "missing_days_prev": _store_days(prev, pw)}

    anomalies = detect_anomalies(tables, threshold, top_n, min_base, dimensions)
    return {
        "period": week.key, "period_label": week.label, "prev_period": pw.key,
        "threshold": threshold, "flat": flat, "tables": tables, "daily": daily, "weekly": weekly,
        "top_items": [r["name"] for r in ranked], "margin_shifts": shifts, "quality": quality,
        "anomalies": anomalies,
    }


_DIM_LABEL = {"region": "区域", "store": "门店", "category": "品类", "rc": "区域品类"}


def _label(dim: str, name: str, code: str | None = None) -> str:
    if dim == "store" and code:
        return f"{name}（{code}）"
    if dim == "region":
        return f"{name}区"
    if dim == "rc":
        r, c = name.split(".")
        return f"{r}区{c}"
    return name


def detect_anomalies(tables: dict[str, list[dict]], threshold: float, top_n: int, min_base: float,
                     dimensions: list[str]) -> list[dict]:
    """环比超过阈值即为异常；门店异常若与所在区域同向异常，则作为区域异常的“驱动因素”合并；
    区域×品类仅在偏离本区域整体趋势时才单独列出，避免重复。"""
    region_wow = {r["name"]: r["sales_wow"] for r in tables["region"]}
    anomalous_regions = {r["name"]: r["sales_wow"] for r in tables["region"]
                         if r.get("sales_wow") is not None and r["sales_prev"] >= min_base and abs(r["sales_wow"]) >= threshold}
    explained: list[dict] = []
    found: list[dict] = []
    for dim in dimensions:
        for r in tables[dim]:
            wow = r.get("sales_wow")
            if wow is None or r["sales_prev"] < min_base or abs(wow) < threshold:
                continue
            if dim == "rc":
                rw = region_wow.get(r["keys"][0]) or 0.0
                if abs(wow - rw) < threshold:
                    continue
            if dim == "category" and anomalous_regions:
                # 剔除同向异常区域后若回到正常范围，说明该品类波动由区域异常带动，归入区域异常而不单列
                same = {g for g, gw in anomalous_regions.items() if (gw > 0) == (wow > 0)}
                rest = [x for x in tables["rc"] if x["keys"][1] == r["name"] and x["keys"][0] not in same]
                s, p = sum(x["sales"] for x in rest), sum(x["sales_prev"] for x in rest)
                if same and p and abs(s / p - 1) < threshold:
                    explained.append({"category": r["name"], "wow": wow, "wow_excl": s / p - 1, "regions": sorted(same),
                                      "placeholder": f"category.{r['name']}.sales_wow"})
                    continue
            found.append({
                "dimension": dim, "dimension_label": _DIM_LABEL[dim], "entity": r["name"],
                "label": _label(dim, r["name"], r.get("code")), "direction": "up" if wow > 0 else "down",
                "sales": r["sales"], "sales_prev": r["sales_prev"], "wow": wow,
                "impact": r["sales"] - r["sales_prev"], "region": r.get("region"),
                "code": r.get("code"), "store_type": r.get("store_type"),
                "placeholders": {"sales": f"{dim}.{r['name']}.sales", "wow": f"{dim}.{r['name']}.sales_wow",
                                 "impact_base": f"{dim}.{r['name']}.sales_prev"},
                "drivers": [],
            })
    regions = {a["entity"]: a for a in found if a["dimension"] == "region"}
    for e in explained:
        for g in e["regions"]:
            if g in regions:
                regions[g].setdefault("related_categories", []).append(e)
    merged = []
    for a in found:
        parent = regions.get(a.get("region") or "")
        if a["dimension"] == "store" and parent and parent["direction"] == a["direction"]:
            parent["drivers"].append({"store": a["entity"], "label": a["label"], "wow": a["wow"], "impact": a["impact"],
                                      "placeholder": a["placeholders"]["wow"]})
            continue
        merged.append(a)
    merged = _group_by_store_type(merged)
    merged.sort(key=lambda a: abs(a["impact"]), reverse=True)
    merged = merged[:top_n]
    for i, a in enumerate(merged, 1):
        a["id"] = f"A{i}"
    return merged


def _group_by_store_type(items: list[dict]) -> list[dict]:
    """未并入区域的门店异常，同店型、同方向的 2 家及以上合并为一条（如“写字楼店（3 家）”回升），
    原因多按店型概括（节后商场客流回落、写字楼复工），避免逐店重复，篇幅也更紧凑。"""
    groups: dict[tuple, list[dict]] = {}
    for a in items:
        if a["dimension"] == "store" and a.get("store_type"):
            groups.setdefault((a["direction"], a["store_type"]), []).append(a)
    out = [a for a in items if not (a["dimension"] == "store" and len(groups.get((a["direction"], a.get("store_type")), [])) > 1)]
    for (direction, kind), members in groups.items():
        if len(members) < 2:
            continue
        sales, prev = sum(m["sales"] for m in members), sum(m["sales_prev"] for m in members)
        out.append({
            "dimension": "store_group", "dimension_label": "门店组", "entity": kind,
            "label": f"{kind}（{len(members)} 家）", "direction": direction, "sales": sales, "sales_prev": prev,
            "wow": sales / prev - 1 if prev else 0.0, "impact": sales - prev, "region": None, "store_type": kind,
            "placeholders": {}, "drivers": [{"store": m["entity"], "label": m["label"], "wow": m["wow"], "impact": m["impact"],
                                             "placeholder": m["placeholders"]["wow"]} for m in members],
        })
    return out


# ---------------------------------------------------------------- 周会纪要

_NUM_ITEM = re.compile(r"^\s*(\d+)\s*[.、．)）]\s*(.*)$")
_CN_SECTION = re.compile(r"^([一二三四五六七八九十]+、)")
_CN_HEADING = re.compile(r"^[一二三四五六七八九十]+、\S.{0,24}$")  # 正文样式写的“一、整体情况”也按章节标题处理
_TAG = re.compile(r"^【([^】]{1,20})】\s*(.*)$")
_SPEAKER = re.compile(r"^([^：:，。；（(]{1,10}[（(][^）)]{1,12}[）)])[：:]\s*(\S.*)$")  # “华南（陈某）：……”
_META = re.compile(r"^(会议时间|时间|会议地点|地点|参会人员|参会人|出席人员|出席|主持人?|记录人?|缺席|抄送)[：:]")
_WEEK = re.compile(r"第\s*(\d{1,2})\s*周")
_YEAR = re.compile(r"((?:19|20)\d{2})\s*年")


def _docx_blocks(path: Path) -> list[tuple[str, Any]]:
    """按文档顺序返回段落与表格：[("p", (样式, 文本)) | ("table", [[单元格]])]。"""
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    doc = Document(str(path))
    out: list[tuple[str, Any]] = []
    for el in doc.element.body.iterchildren():
        tag = el.tag.rsplit("}", 1)[-1]
        if tag == "p":
            p = Paragraph(el, doc)
            out.append(("p", ((p.style.name if p.style is not None else "").lower(), p.text)))
        elif tag == "tbl":
            out.append(("table", [[c.text.strip() for c in r.cells] for r in Table(el, doc).rows]))
    return out


def _table_rows(rows: list[list[str]]) -> list[str]:
    """表格（如行动项：事项 / 负责人 / 截止日期）每行转成一条纪要：“事项（负责人：…，截止日期：…）”。"""
    rows = [r for r in rows if any(r)]
    if len(rows) < 2:
        return [" ".join(r) for r in rows]
    head = rows[0]
    out = []
    for r in rows[1:]:
        rest = "，".join(f"{h}：{v}" for h, v in zip(head[1:], r[1:]) if v)
        out.append(f"{r[0]}（{rest}）" if rest else r[0])
    return out


def _minutes_lines(path: Path) -> list[tuple[str, str]]:
    """返回 [(类型, 文本)]，类型为 title（一级标题）、section（二级及以下标题）或 line。段落内的换行拆成多行，表格每行一条。"""
    ext = path.suffix.lower()
    out: list[tuple[str, str]] = []
    if ext == ".docx":
        for kind, val in _docx_blocks(path):
            if kind == "table":
                out += [("line", t) for t in _table_rows(val)]
                continue
            style, text = val
            if style == "title" or style == "heading 1":
                out.append(("title", text.strip()))
            elif style.startswith("heading") or _CN_HEADING.match(text.strip()):
                out.append(("section", text.strip()))
            else:
                out += [("line", ln) for ln in text.splitlines()]
    elif ext in (".md", ".txt"):
        for ln in path.read_text(encoding="utf-8").splitlines():
            if re.match(r"^#\s", ln):
                out.append(("title", ln.lstrip("#").strip()))
            elif ln.startswith("##") or _CN_HEADING.match(ln.strip()):
                out.append(("section", ln.lstrip("#").strip()))
            else:
                out.append(("line", ln))
    else:
        raise DataError("INVALID_INPUT", f"不支持的周会纪要格式：{path.suffix}（支持 docx / md）")
    return out


def minutes_meta(path: Path) -> dict[str, Any]:
    """纪要标题中的周次（如“经营周会纪要（2026 年第 44 周）”），用于核对是否与统计周期一致、报告中注明出处。"""
    for kind, text in _minutes_lines(path):
        if kind == "section":
            break
        if kind in ("title", "line") and "纪要" in text and (m := _WEEK.search(text)):
            y = _YEAR.search(text)
            return {"title": text.strip(), "week": int(m.group(1)), "year": int(y.group(1)) if y else None}
    return {}


def read_minutes(path: Path) -> list[dict[str, Any]]:
    """把纪要拆成可引用的条目：{no 内部编号, ref 原文位置（如“一、1”）, header 小标题, text 正文, section 所在章节}。

    兼容常见写法：段落内软换行、“华东大区（负责人：…）：”这类小标题行、“华南（陈某）：……”这类发言、
    一个段落里写多条编号事项、二级标题或正文样式的“一、”分节、表格形式的行动项。
    纪要标题、会议时间、参会人员等会议信息不作为条目。
    """
    meta = minutes_meta(path)
    items: list[dict[str, Any]] = []
    section, header, cur_no, auto_no, started = "", None, None, 0, False
    for kind, raw in _minutes_lines(path):
        if kind == "title":
            continue
        if kind == "section":
            section, header, cur_no, auto_no, started = raw, None, None, 0, True
            continue
        line = raw.strip()
        if not line:
            continue
        if not started and (_META.match(line) or line == meta.get("title")):
            continue
        m = _NUM_ITEM.match(line)
        if m:
            cur_no, line, header = int(m.group(1)), m.group(2).strip(), None
        line = re.sub(r"^[-*•]\s*", "", line)
        tag = None
        b = _TAG.match(line)
        if b and b.group(2).strip():  # “【营运中心】审批通过…”：段首【】是该条的标签，不属于正文
            tag, line = b.group(1), b.group(2).strip()
        if not line:
            continue
        if line.endswith(("：", ":")) and len(line) <= 40:  # 小标题行，作为后续正文的标题
            header = line.rstrip("：:")
            continue
        own_header = header
        sp = _SPEAKER.match(line)
        if sp:  # 发言人只属于这一条
            own_header, line = sp.group(1), sp.group(2).strip()
        if m is None and header is None:
            auto_no += 1
            num = auto_no if cur_no is None else cur_no
        else:
            num = cur_no if cur_no is not None else auto_no + 1
        sec = _CN_SECTION.match(section)
        ref = f"{sec.group(1)}{num}" if sec else (f"{section} 第{num}条" if section else f"第{num}条")
        item = {"no": len(items) + 1, "ref": ref, "section": section, "header": own_header, "tag": tag, "text": line}
        if meta.get("week"):
            item["week"] = meta["week"]
        items.append(item)
    return items


def minutes_pii(paras: list[dict]) -> bool:
    return any(PHONE_RE.search(p["text"]) or IDCARD_RE.search(p["text"]) for p in paras)


def search_minutes(paras: list[dict], keywords: list[str]) -> list[dict]:
    scored = []
    for p in paras:
        full = f"{p.get('header') or ''} {p.get('tag') or ''} {p['text']}"
        score = sum(full.count(k) for k in keywords if k)
        if score:
            scored.append((score, p))
    scored.sort(key=lambda x: (-x[0], x[1]["no"]))
    return [p for _, p in scored]
