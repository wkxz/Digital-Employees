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

def read_table(path: Path) -> pd.DataFrame:
    ext = path.suffix.lower()
    if ext in (".xlsx", ".xls"):
        return pd.read_excel(path)
    if ext == ".csv":
        for enc in ("utf-8-sig", "gbk"):
            try:
                return pd.read_csv(path, encoding=enc)
            except UnicodeDecodeError:
                continue
    raise DataError("INVALID_INPUT", f"不支持的销售数据格式：{path.suffix}（支持 xlsx / csv）")


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


def load_sales(path: Path, aliases: dict[str, list[str]], required: list[str]) -> tuple[pd.DataFrame, dict[str, Any]]:
    raw = read_table(path)
    df, mapping = normalize_columns(raw, aliases)
    missing = [c for c in required if c not in df.columns]
    info: dict[str, Any] = {
        "rows": int(len(df)), "columns": [str(c) for c in raw.columns], "alias_mapping": mapping,
        "missing_columns": missing, "pii_columns": find_pii(raw),
    }
    if not missing:
        dates = pd.to_datetime(df["日期"], errors="coerce").dropna()
        if len(dates):
            info["date_range"] = [dates.min().date().isoformat(), dates.max().date().isoformat()]
            iso = dates.dt.isocalendar()
            info["weeks"] = sorted({f"{y}-W{w:02d}" for y, w in zip(iso["year"], iso["week"])})
    return df, info


KEY = ["日期", "门店", "品类"]


def load_sales_many(paths: list[Path], aliases: dict[str, list[str]], required: list[str]
                    ) -> tuple[pd.DataFrame, dict[str, Any]]:
    """读取并合并多份销售明细（如先传本周、再补传上周）。各文件分别识别列名；
    同一 日期+门店+品类 在多份文件中都出现时，以后上传的文件为准（补传更正版即可覆盖）。"""
    loaded = []
    for i, p in enumerate(paths):
        df, info = load_sales(p, aliases, required)
        df["_src"] = i
        dates = set(pd.to_datetime(df["日期"], errors="coerce").dropna().dt.date) if "日期" in df.columns else set()
        loaded.append((p, df, info, dates))
    # 更正版重传：后上传的文件列齐全时，丢弃先前缺列的文件；后上传的文件覆盖了先前文件的全部日期时，先前文件整体作废
    superseded = []
    for i, (p, _, info, dates) in enumerate(loaded):
        later = [x for x in loaded[i + 1:] if not x[2]["missing_columns"]]
        if later and (info["missing_columns"] or any(dates and dates <= x[3] for x in later)):
            superseded.append(p.name.split("_", 1)[-1])
    kept = [x for x in loaded if x[0].name.split("_", 1)[-1] not in superseded] or loaded[-1:]
    paths = [x[0] for x in kept]
    frames, infos = [x[1] for x in kept], [x[2] for x in kept]
    missing = sorted({c for inf in infos for c in inf["missing_columns"]})
    df = pd.concat(frames, ignore_index=True)
    overridden = 0
    if len(paths) > 1 and not missing:
        key = df[KEY].assign(日期=pd.to_datetime(df["日期"], errors="coerce"))
        latest = df.groupby([key[c] for c in KEY], dropna=False)["_src"].transform("max")
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
    return f"{d} {r.get('门店', '')} {r.get('品类', '') if pd.notna(r.get('品类')) else ''}".strip()


def clean_sales(df: pd.DataFrame) -> tuple[pd.DataFrame, dict[str, Any]]:
    """清洗规则（每一处处理都记入 issues，供附录和审核人逐条核对）：
    - 完全相同的重复行：删除（按 日期+门店+品类 判定，销售额为 0 的停业记录不会被误判为重复）
    - 关键字段为空、销售额为负（含 -9999 之类的占位值）：整行剔除
    - 销售额正常但销量为负：保留销售额，该行销量不计入
    - 销售额为 0：保留（多为停业），按门店汇总日期，便于与纪要核对
    """
    total = len(df)
    df = df.copy()
    df["日期"] = pd.to_datetime(df["日期"], errors="coerce")
    for c in ("销售额", "销量", "订单数"):
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    issues: list[dict[str, Any]] = []
    dup_mask = df.duplicated()
    for _, r in df[dup_mask].iterrows():
        issues.append({"row": _row_desc(r), "problem": "与另一行完全相同", "action": "删除重复行"})
    df = df[~dup_mask]
    null_mask = df[["日期", "门店", "区域", "品类", "销售额"]].isna().any(axis=1)
    for _, r in df[null_mask].iterrows():
        cols = [c for c in ("日期", "门店", "区域", "品类", "销售额") if pd.isna(r[c])]
        issues.append({"row": _row_desc(r), "problem": f"{'、'.join(cols)}为空", "action": "整行剔除"})
    neg_sales = (df["销售额"] < 0) & ~null_mask
    for _, r in df[neg_sales].iterrows():
        v = r["销售额"]
        hint = "，疑似占位值" if v in (-9999, -999, -99999, -1) else ""
        issues.append({"row": _row_desc(r), "problem": f"销售额为 {v:g}{hint}", "action": "整行剔除"})
    df = df[~null_mask & ~neg_sales].copy()
    bad_qty = df["销量"].isna() | (df["销量"] < 0)
    for _, r in df[bad_qty].iterrows():
        issues.append({"row": _row_desc(r), "problem": f"销量为 {r['销量']:g}" if pd.notna(r["销量"]) else "销量为空",
                       "action": "保留销售额，该行销量不计入"})
    df.loc[bad_qty, "销量"] = float("nan")
    zero = df[df["销售额"] == 0]
    zero_days = {store: sorted(g["日期"].dt.strftime("%m-%d").unique()) for store, g in zero.groupby("门店")}
    dropped = int(dup_mask.sum() + null_mask.sum() + neg_sales.sum())
    return df, {
        "total_rows": total, "duplicates": int(dup_mask.sum()), "null_rows": int(null_mask.sum()),
        "negative_rows": int(neg_sales.sum()), "bad_qty_rows": int(bad_qty.sum()), "zero_sales_rows": int(len(zero)),
        "zero_sales_days": zero_days, "kept_rows": int(len(df)),
        "bad_ratio": round((dropped + int(bad_qty.sum())) / total, 4) if total else 0.0, "issues": issues,
    }


# ---------------------------------------------------------------- KPI

def _agg(df: pd.DataFrame) -> dict[str, float]:
    out = {"sales": float(df["销售额"].sum()), "qty": float(df["销量"].sum())}
    if "订单数" in df.columns:
        out["orders"] = float(df["订单数"].sum())
        out["atv"] = out["sales"] / out["orders"] if out["orders"] else 0.0
    return out


def _with_wow(cur: dict[str, float], prev: dict[str, float]) -> dict[str, float]:
    out = dict(cur)
    for k, v in cur.items():
        p = prev.get(k, 0.0)
        out[f"{k}_prev"] = p
        out[f"{k}_wow"] = (v / p - 1) if p else None
    return out


def compute_kpis(df: pd.DataFrame, week: Week, threshold: float, top_n: int, min_base: float,
                 dimensions: list[str]) -> dict[str, Any]:
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

    flat: dict[str, Any] = {}
    total = _with_wow(_agg(cur), _agg(prev))
    total["stores"] = int(cur["门店"].nunique())
    for k, v in total.items():
        flat[f"total.{k}"] = v

    groups = {"region": ["区域"], "store": ["门店"], "category": ["品类"], "rc": ["区域", "品类"]}
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
            for k, v in m.items():
                flat[f"{dim}.{name}.{k}"] = v
            rows.append({"name": name, "keys": list(key), **m})
        if dim == "store":
            region_of = cur.drop_duplicates("门店").set_index("门店")["区域"].to_dict()
            for r in rows:
                r["region"] = region_of.get(r["name"])
        tables[dim] = rows

    daily = []
    for i, d in enumerate(week.days()):
        pd_ = pw.days()[i]
        daily.append({
            "date": d.isoformat(), "weekday": "一二三四五六日"[i],
            "sales": float(cur.loc[cur["日期"] == pd.Timestamp(d), "销售额"].sum()),
            "sales_prev": float(prev.loc[prev["日期"] == pd.Timestamp(pd_), "销售额"].sum()),
        })

    anomalies = detect_anomalies(tables, threshold, top_n, min_base, dimensions)
    return {
        "period": week.key, "period_label": week.label, "prev_period": pw.key,
        "threshold": threshold, "flat": flat, "tables": tables, "daily": daily, "anomalies": anomalies,
    }


_DIM_LABEL = {"region": "区域", "store": "门店", "category": "品类", "rc": "区域品类"}


def _label(dim: str, name: str) -> str:
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
                "label": _label(dim, r["name"]), "direction": "up" if wow > 0 else "down",
                "sales": r["sales"], "sales_prev": r["sales_prev"], "wow": wow,
                "impact": r["sales"] - r["sales_prev"], "region": r.get("region"),
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
            parent["drivers"].append({"store": a["entity"], "wow": a["wow"], "impact": a["impact"],
                                      "placeholder": a["placeholders"]["wow"]})
            continue
        merged.append(a)
    merged.sort(key=lambda a: abs(a["impact"]), reverse=True)
    merged = merged[:top_n]
    for i, a in enumerate(merged, 1):
        a["id"] = f"A{i}"
    return merged


# ---------------------------------------------------------------- 周会纪要

_NUM_ITEM = re.compile(r"^\s*(\d+)\s*[.、．)）]\s*(.*)$")
_CN_SECTION = re.compile(r"^([一二三四五六七八九十]+、)")


def _minutes_lines(path: Path) -> list[tuple[str, str]]:
    """返回 [(类型, 文本)]，类型为 section（二级及以下标题）或 line。段落内的换行拆成多行。"""
    ext = path.suffix.lower()
    out: list[tuple[str, str]] = []
    if ext == ".docx":
        for p in Document(str(path)).paragraphs:
            style = (p.style.name if p.style is not None else "").lower()
            if style == "title" or style == "heading 1":
                continue
            if style.startswith("heading"):
                out.append(("section", p.text.strip()))
            else:
                out += [("line", ln) for ln in p.text.splitlines()]
    elif ext in (".md", ".txt"):
        for ln in path.read_text(encoding="utf-8").splitlines():
            if re.match(r"^#\s", ln):
                continue
            if ln.startswith("##"):
                out.append(("section", ln.lstrip("#").strip()))
            else:
                out.append(("line", ln))
    else:
        raise DataError("INVALID_INPUT", f"不支持的周会纪要格式：{path.suffix}（支持 docx / md）")
    return out


def read_minutes(path: Path) -> list[dict[str, Any]]:
    """把纪要拆成可引用的条目：{no 内部编号, ref 原文位置（如“一、1”）, header 小标题, text 正文, section 所在章节}。

    兼容常见写法：段落内软换行、“华东大区（负责人：…）：”这类小标题行、一个段落里写多条编号事项、二级标题分节。
    """
    items: list[dict[str, Any]] = []
    section, header, cur_no, auto_no = "", None, None, 0
    for kind, raw in _minutes_lines(path):
        if kind == "section":
            section, header, cur_no, auto_no = raw, None, None, 0
            continue
        line = raw.strip()
        if not line:
            continue
        m = _NUM_ITEM.match(line)
        if m:
            cur_no, line, header = int(m.group(1)), m.group(2).strip(), None
        line = re.sub(r"^[-*•]\s*", "", line)
        if not line:
            continue
        if line.endswith(("：", ":")) and len(line) <= 40:  # 小标题行，作为后续正文的标题
            header = line.rstrip("：:")
            continue
        if m is None and header is None:
            auto_no += 1
            num = auto_no if cur_no is None else cur_no
        else:
            num = cur_no if cur_no is not None else auto_no + 1
        sec = _CN_SECTION.match(section)
        ref = f"{sec.group(1)}{num}" if sec else (f"{section} 第{num}条" if section else f"第{num}条")
        items.append({"no": len(items) + 1, "ref": ref, "section": section, "header": header, "text": line})
    return items


def minutes_pii(paras: list[dict]) -> bool:
    return any(PHONE_RE.search(p["text"]) or IDCARD_RE.search(p["text"]) for p in paras)


def search_minutes(paras: list[dict], keywords: list[str]) -> list[dict]:
    scored = []
    for p in paras:
        full = f"{p.get('header') or ''} {p['text']}"
        score = sum(full.count(k) for k in keywords if k)
        if score:
            scored.append((score, p))
    scored.sort(key=lambda x: (-x[0], x[1]["no"]))
    return [p for _, p in scored]
