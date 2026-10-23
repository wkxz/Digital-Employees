"""模型步骤的输入（由程序从 KPI / 纪要 / 审核意见组装）与输出 Schema，以及审核意见解析。"""
from __future__ import annotations

import re
from typing import Any

from .placeholders import fmt_value

_STR_LIST = {"type": "array", "items": {"type": "string", "minLength": 1}}
ANOMALY_ITEM = {
    "type": "object",
    "required": ["id", "phenomenon", "cause", "evidence", "suggestion"],
    "properties": {"id": {"type": "string"}, "phenomenon": {"type": "string"}, "cause": {"type": "string"},
                   "brief": {"type": "string", "description": "摘要用的一句话原因，不含数字"},
                   "evidence": {"type": "array", "items": {"type": "integer"}}, "suggestion": {"type": "string"}},
}
DRAFT_SECTIONS = ["summary", "overall", "regions", "categories", "next_week"]

OUTPUT_SCHEMAS: dict[str, dict[str, Any]] = {
    "analysis": {"type": "object", "required": ["anomalies"],
                 "properties": {"anomalies": {"type": "array", "items": ANOMALY_ITEM}}},
    "draft": {"type": "object", "required": DRAFT_SECTIONS, "properties": {k: _STR_LIST for k in DRAFT_SECTIONS}},
    "fix": {"type": "object", "required": DRAFT_SECTIONS + ["anomalies"],
            "properties": {**{k: _STR_LIST for k in DRAFT_SECTIONS},
                           "anomalies": {"type": "array", "items": ANOMALY_ITEM}}},
}


def _direction(v: float | None) -> str:
    if v is None or abs(v) < 0.005:
        return "flat"
    return "up" if v > 0 else "down"


def store_core(name: str) -> str:
    base = name[:-1] if name.endswith("店") else name
    return base[2:] if len(base) >= 4 else base


def overview(kpis: dict[str, Any]) -> dict[str, Any]:
    f, t = kpis["flat"], kpis["tables"]

    def rows(dim: str) -> list[dict]:
        ranked = sorted(t[dim], key=lambda r: r["sales"], reverse=True)
        return [{"name": r["name"], "sales": fmt_value("sales", r["sales"]), "sales_wow": fmt_value("x_wow", r["sales_wow"]),
                 "direction": _direction(r["sales_wow"]), "sales_rank": i} for i, r in enumerate(ranked, 1)]

    stores = [r for r in t["store"] if r["sales_wow"] is not None]
    best = max(stores, key=lambda r: r["sales_wow"]) if stores else None
    worst = min(stores, key=lambda r: r["sales_wow"]) if stores else None
    return {
        "total": {"sales": fmt_value("sales", f["total.sales"]), "sales_wow": fmt_value("x_wow", f["total.sales_wow"]),
                  "sales_direction": _direction(f["total.sales_wow"]), "sales_wow_value": f["total.sales_wow"]},
        "has_orders": "total.orders" in f,
        "regions": rows("region"), "categories": rows("category"),
        # 日趋势只能依据这里的数据描述，不得推测
        "daily": [{"date": d["date"], "weekday": f"周{d['weekday']}", "sales": fmt_value("sales", d["sales"]),
                   "last_week_same_day": fmt_value("sales", d["sales_prev"]),
                   "change": fmt_value("x_wow", d["sales"] / d["sales_prev"] - 1 if d["sales_prev"] else None)}
                  for d in kpis.get("daily", [])],
        "best_store": {"name": best["name"], "sales_wow": fmt_value("x_wow", best["sales_wow"])} if best else None,
        "worst_store": {"name": worst["name"], "sales_wow": fmt_value("x_wow", worst["sales_wow"])} if worst else None,
    }


def anomaly_inputs(kpis: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for a in kpis["anomalies"]:
        keys = a["keys"] if "keys" in a else a["entity"].split(".")
        if a["dimension"] == "store":
            hints = [a["entity"], store_core(a["entity"])]
        else:
            hints = list(keys)
        out.append({
            "id": a["id"], "label": a["label"], "dimension": a["dimension_label"], "entity": a["entity"],
            "direction": a["direction"], "wow": fmt_value("x_wow", a["wow"]), "impact": fmt_value("impact", a["impact"]),
            "placeholders": {"sales": f"anomaly.{a['id']}.sales", "wow": f"anomaly.{a['id']}.sales_wow",
                             "impact": f"anomaly.{a['id']}.impact"},
            "drivers": [{"store": d["store"], "wow": fmt_value("x_wow", d["wow"]), "placeholder": d["placeholder"]}
                        for d in a["drivers"]],
            "related_categories": [{"category": c["category"], "wow": fmt_value("x_wow", c["wow"]),
                                    "wow_excluding_region": fmt_value("x_wow", c["wow_excl"]),
                                    "direction": _direction(c["wow"]), "placeholder": c["placeholder"]}
                                   for c in a.get("related_categories", [])],
            "search_hints": hints,
            "driver_hints": [store_core(d["store"]) for d in a["drivers"]],
        })
    return out


PLACEHOLDER_HELP = (
    "占位符格式 {{键}}。常用键：total.sales / total.sales_prev / total.sales_wow / total.qty / total.qty_wow / "
    "total.orders / total.atv / total.atv_wow / total.stores；region.<区域>.sales|sales_wow|share；"
    "store.<门店>.sales|sales_wow；category.<品类>.sales|sales_wow|share；anomaly.<A1>.sales|sales_wow|impact。"
    "跟在“增长/下降”后面时写 {{键|abs}}，去掉正负号。"
)


ACTION_SECTION = re.compile(r"决策|行动|计划|下周|待办|安排")


def action_items(minutes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """纪要中的决策与下周行动项（所在章节或小标题含“决策/行动/计划/下周”，或条目以“下周计划”开头）。"""
    out = []
    for m in minutes:
        where = f"{m.get('section', '')} {m.get('header') or ''}"
        if ACTION_SECTION.search(where) or m["text"].startswith(("下周计划", "行动项", "决议")):
            out.append({"no": m["no"], "ref": m.get("ref"), "text": m["text"].rstrip("；;。")})
    return out


def placeholder_table(kpis: dict[str, Any]) -> dict[str, str]:
    """写作常用的占位符及其当前值，直接给模型，免去逐个 query_kpi 查询。门店只给环比最高 / 最低各 3 家及异常相关门店。"""
    from .render import anomaly_flat
    flat = anomaly_flat(kpis)
    stores = sorted((r for r in kpis["tables"]["store"] if r["sales_wow"] is not None), key=lambda r: r["sales_wow"])
    keep_stores = {r["name"] for r in stores[:3] + stores[-3:]}
    keep_stores |= {d["store"] for a in kpis["anomalies"] for d in a["drivers"]}
    out = {}
    for k, v in flat.items():
        head = k.split(".")[0]
        if head == "store" and k.split(".")[1] not in keep_stores:
            continue
        if head == "rc" and not any(a["dimension"] == "rc" and k.startswith(f"rc.{a['entity']}.") for a in kpis["anomalies"]):
            continue
        if k.rsplit(".", 1)[-1] in ("sales", "sales_prev", "sales_wow", "share", "qty", "qty_wow", "orders",
                                     "orders_wow", "atv", "atv_wow", "stores", "impact"):
            out[k] = fmt_value(k, v)
    return out


def entity_names(kpis: dict[str, Any]) -> list[str]:
    t = kpis["tables"]
    return [r["name"] for d in ("region", "store", "category") for r in t[d]]


def parse_feedback(comments: str, cfg: dict[str, Any], kpis: dict[str, Any]) -> list[dict[str, Any]]:
    """把审核意见拆成程序可校验的待办：{section, instruction, keywords, metrics, max_items}。
    规则来自员工配置 feedback.section_aliases / metric_keywords。"""
    aliases: dict[str, list[str]] = cfg.get("section_aliases", {})
    metric_kw: dict[str, str] = cfg.get("metric_keywords", {})
    names = entity_names(kpis)
    todos = []
    for item in filter(None, (s.strip() for s in re.split(r"[\n；;。]", comments))):
        section = next((sid for sid, words in aliases.items() if any(w in item for w in words)), "*")
        kws = [n for n in names if n in item or (len(n) > 2 and n.rstrip("店") in item)]
        metrics = {k: v for k, v in metric_kw.items() if k in item}
        todo: dict[str, Any] = {"section": section, "instruction": item, "keywords": kws + list(metrics),
                                "metrics": metrics}
        m = re.search(r"(\d+)\s*条", item)
        if m and section in ("summary", "next_week"):
            todo["max_items"] = int(m.group(1))
        todos.append(todo)
    return todos
