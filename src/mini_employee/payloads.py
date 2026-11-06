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
DRAFT_SECTIONS = ["summary", "overall", "regions", "categories", "next_week"]  # 默认章节；实际以员工配置为准
TEXT_KINDS = ("paragraphs", "bullets")


def draft_sections(report_cfg: dict[str, Any] | None) -> list[str]:
    """由模型撰写文字的章节 = 员工配置 report.sections 中 kind 为 paragraphs / bullets 的章节。
    新增章节只需在配置里加一行，撰写与修正步骤的输出 Schema 随之变化。"""
    if not report_cfg:
        return list(DRAFT_SECTIONS)
    return [s["id"] for s in report_cfg["sections"] if s.get("kind", "paragraphs") in TEXT_KINDS]


def output_schema(purpose: str, report_cfg: dict[str, Any] | None = None) -> dict[str, Any]:
    secs = draft_sections(report_cfg)
    if purpose == "analysis":
        return {"type": "object", "required": ["anomalies"],
                "properties": {"anomalies": {"type": "array", "items": ANOMALY_ITEM}}}
    if purpose == "draft":
        return {"type": "object", "required": secs, "properties": {k: _STR_LIST for k in secs}}
    if purpose == "fix":
        return {"type": "object", "required": secs + ["anomalies"],
                "properties": {**{k: _STR_LIST for k in secs}, "anomalies": {"type": "array", "items": ANOMALY_ITEM}}}
    raise KeyError(purpose)


OUTPUT_SCHEMAS: dict[str, dict[str, Any]] = {k: output_schema(k) for k in ("analysis", "draft", "fix")}  # 默认章节下的 Schema


def form_fields(form: dict[str, Any], form_cfg: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """发起人在表单中填写的其他字段（文件与统计周期除外），连同配置里的写作要求一起交给模型。
    表单加字段时在配置里写上 guide（怎么用）和 section（体现在哪一章），模型照做，质检按 section 校验。"""
    out = []
    for f in form_cfg:
        v = form.get(f["key"])
        if f["type"] in ("file", "iso_week") or v in (None, "", []):
            continue
        out.append({k: x for k, x in {"key": f["key"], "label": f["label"], "value": v, "guide": f.get("guide"),
                                      "section": f.get("section")}.items() if x is not None})
    return out


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
    items = {r["name"]: r for r in t.get("item", [])}
    return {
        "total": {"sales": fmt_value("sales", f["total.sales"]), "sales_wow": fmt_value("x_wow", f["total.sales_wow"]),
                  "sales_direction": _direction(f["total.sales_wow"]), "sales_wow_value": f["total.sales_wow"]},
        "has_orders": "total.orders" in f,
        "has_margin": "total.gross_margin" in f, "has_delivery": "total.delivery_share" in f,
        "has_target": "total.target_rate" in f,
        "region_targets": [{"name": r["name"], "target_rate": fmt_value("target_rate", r.get("target_rate")),
                            "gross_margin": fmt_value("gross_margin", r.get("gross_margin"))}
                           for r in t["region"] if r.get("target_rate") is not None],
        # 实收前几名单品（程序排序），写作时按此顺序点名
        "top_items": [{"rank": i, "name": n, "sales_wow": fmt_value("x_wow", items[n]["sales_wow"])}
                      for i, n in enumerate(kpis.get("top_items", []), 1) if n in items],
        # 区域×品类毛利率明显变动（如促销让利导致毛利率下降）
        "margin_shifts": [{"region": m["region"], "category": m["category"],
                           "now": fmt_value("gross_margin", m["cur"]), "last_week": fmt_value("gross_margin", m["prev"]),
                           "change": fmt_value("x_chg", m["chg"]), "placeholder": m["placeholder"]}
                          for m in kpis.get("margin_shifts", [])],
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
            hints = [a["entity"], store_core(a["entity"])] + ([a["store_type"]] if a.get("store_type") else [])
        else:
            hints = list(keys)
        out.append({
            "id": a["id"], "label": a["label"], "dimension": a["dimension_label"], "entity": a["entity"],
            **({"store_type": a["store_type"]} if a.get("store_type") else {}),
            "direction": a["direction"], "wow": fmt_value("x_wow", a["wow"]), "impact": fmt_value("impact", a["impact"]),
            "placeholders": {"sales": f"anomaly.{a['id']}.sales", "wow": f"anomaly.{a['id']}.sales_wow",
                             "impact": f"anomaly.{a['id']}.impact"},
            "drivers": [{"store": d.get("label", d["store"]), "wow": fmt_value("x_wow", d["wow"]),
                         "placeholder": d["placeholder"]} for d in a["drivers"]],
            "related_categories": [{"category": c["category"], "wow": fmt_value("x_wow", c["wow"]),
                                    "wow_excluding_region": fmt_value("x_wow", c["wow_excl"]),
                                    "direction": _direction(c["wow"]), "placeholder": c["placeholder"]}
                                   for c in a.get("related_categories", [])],
            "search_hints": hints,
            "driver_hints": [store_core(d["store"]) for d in a["drivers"]],
        })
    return out


PLACEHOLDER_HELP = (
    "占位符格式 {{键}}。常用键：total.sales（实收）/ total.sales_prev / total.sales_wow / total.gross_margin（毛利率）/ "
    "total.delivery_share（外卖占比）/ total.target / total.target_rate（目标达成率）/ total.qty / total.stores；"
    "region.<区域>.sales|sales_wow|share|gross_margin|target_rate；store.<门店>.sales|sales_wow；"
    "category.<品类>.sales|sales_wow|share|gross_margin；item.<单品>.sales|sales_wow|share；"
    "rc.<区域>.<品类>.gross_margin|gross_margin_prev|gross_margin_chg；anomaly.<A1>.sales|sales_wow|impact。"
    "*_chg 是比率的变动（百分点）。跟在“增长/下降”后面时写 {{键|abs}}，去掉正负号。没有的指标（如客单价、同比）不要写。"
)


ACTION_SECTION = re.compile(r"决策|行动|计划|下周|待办|安排")


def action_items(minutes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """纪要中的决策与下周行动项（所在章节或小标题含“决策/行动/计划/下周”，或条目以“下周计划”开头）。"""
    out = []
    for m in minutes:
        where = f"{m.get('section', '')} {m.get('header') or ''}"
        if ACTION_SECTION.search(where) or m["text"].startswith(("下周计划", "行动项", "决议")):
            owner = m.get("tag") or ""  # 【营运中心】等标签是行动项的负责部门：“营运中心审批通过……”
            out.append({"no": m["no"], "ref": m.get("ref"), "text": owner + m["text"].rstrip("；;。")})
    return out


def placeholder_table(kpis: dict[str, Any]) -> dict[str, str]:
    """写作常用的占位符及其当前值，直接给模型，免去逐个 query_kpi 查询。
    门店只给环比最高 / 最低各 3 家、环比超阈值及异常相关的门店；区域×品类只给异常或毛利率明显变动的组合。"""
    from .render import anomaly_flat
    flat = anomaly_flat(kpis)
    stores = sorted((r for r in kpis["tables"]["store"] if r["sales_wow"] is not None), key=lambda r: r["sales_wow"])
    keep_stores = {r["name"] for r in stores[:3] + stores[-3:]}
    keep_stores |= {d["store"] for a in kpis["anomalies"] for d in a["drivers"]}
    keep_stores |= {r["name"] for r in stores if abs(r["sales_wow"]) > kpis["threshold"]}
    shifted = {m["placeholder"].rsplit(".", 1)[0] for m in kpis.get("margin_shifts", [])}
    out = {}
    for k, v in flat.items():
        head = k.split(".")[0]
        if head == "store" and k.split(".")[1] not in keep_stores:
            continue
        if head == "rc" and not any(a["dimension"] == "rc" and k.startswith(f"rc.{a['entity']}.") for a in kpis["anomalies"]) \
                and k.rsplit(".", 1)[0] not in shifted:
            continue
        if head == "rc" and k.rsplit(".", 1)[-1].startswith("gross_margin"):
            out[k] = fmt_value(k, v)
            continue
        if head == "item" and k.split(".")[1] not in kpis.get("top_items", []):
            continue
        if k.rsplit(".", 1)[-1] in ("sales", "sales_prev", "sales_wow", "share", "qty", "qty_wow", "orders",
                                     "orders_wow", "atv", "atv_wow", "stores", "impact", "gross_margin",
                                     "gross_margin_prev", "gross_margin_chg", "delivery_share", "target",
                                     "target_rate"):
            out[k] = fmt_value(k, v)
    return out


def entity_names(kpis: dict[str, Any]) -> list[str]:
    t = kpis["tables"]
    return [r["name"] for d in ("region", "store", "category") for r in t[d]]


CAUSE_WORDS = ["原因", "归因", "为什么", "说清", "解释", "依据"]  # 员工配置 feedback.cause_words 可覆盖


def _short_names(name: str) -> list[str]:
    """门店名的常见简称：上海静安店 → 静安店、静安。用于在意见和纪要中认出门店。"""
    out = [name]
    if name.endswith("店") and len(name) > 4:
        out += [name[2:], name[2:-1]]
    return out


def anomaly_targets(kpis: dict[str, Any], kws: list[str], minutes: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """意见点名的区域 / 门店 → 对应的异常条目，以及纪要中与之相关的条目编号（提到该区域、带动门店或该门店）。"""
    out = []
    for a in kpis.get("anomalies", []):
        stores = [d["store"] for d in a.get("drivers") or [] if d.get("store")]
        if not (a["entity"] in kws or a.get("region") in kws or any(st in kws for st in stores)):
            continue
        tokens = {t for n in [a["entity"], *stores] for t in _short_names(n)}
        if a.get("region"):
            tokens.add(a["region"])
        rel = [m["no"] for m in minutes or []
               if any(t in (m.get("text") or "") or t in (m.get("header") or "") for t in tokens)]
        out.append({"id": a["id"], "label": a["label"], "minutes": rel})
    return out


def _norm(s: str) -> str:
    return re.sub(r"[\s，,。.；;：:、“”\"'（）()]", "", s)


def parse_feedback(comments: str, cfg: dict[str, Any], kpis: dict[str, Any],
                   minutes: list[dict[str, Any]] | None = None, history: list[str] | None = None) -> list[dict[str, Any]]:
    """把审核意见拆成程序可校验的待办：{section, instruction, keywords, metrics, max_items}。
    规则来自员工配置 feedback.section_aliases / metric_keywords / cause_words。
    要求说清某区域 / 门店异常原因的，再定位到具体异常条目（targets），并列出纪要中相关的条目，
    质检时逐条核对原因是否真的补充了依据，而不是只换个说法。
    history：本任务之前各轮的审核意见原文，同一条意见再次出现时记 repeat（第几次提出）。"""
    aliases: dict[str, list[str]] = cfg.get("section_aliases", {})
    metric_kw: dict[str, str] = cfg.get("metric_keywords", {})
    cause_words: list[str] = cfg.get("cause_words", CAUSE_WORDS)
    names = entity_names(kpis)
    seen = [_norm(x) for h in history or [] for x in re.split(r"[\n；;。]", h) if x.strip()]
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
        if any(w in item for w in cause_words):
            targets = anomaly_targets(kpis, kws, minutes)
            if targets:
                todo.update(section="anomalies", targets=targets, need_minutes="纪要" in item)
        n = seen.count(_norm(item))
        if n:
            todo["repeat"] = n + 1
        todos.append(todo)
    return todos
