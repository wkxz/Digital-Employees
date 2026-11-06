"""把“模型写的文字（含占位符）+ 程序算的表格/图表”组装成交付文件（docx / md）。

先组装成与格式无关的块列表，再分别写成 Word 和 Markdown，新增交付格式只需加一个 writer。
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml.ns import qn
from docx.shared import Cm, Pt, RGBColor

from .placeholders import fmt_value, resolve
from .reconcile import extra_notes as reconcile_extra
from .reconcile import notes as reconcile_notes

Block = tuple[str, Any]


class _Default(dict):
    """配置里引用了表单未填的字段时显示为空，而不是报错。"""

    def __missing__(self, key: str) -> str:
        return ""


def anomaly_flat(kpis: dict[str, Any]) -> dict[str, Any]:
    flat = dict(kpis["flat"])
    for a in kpis["anomalies"]:
        flat[f"anomaly.{a['id']}.sales_wow"] = a["wow"]
        flat[f"anomaly.{a['id']}.impact"] = a["impact"]
        flat[f"anomaly.{a['id']}.sales"] = a["sales"]
    return flat


def _store_names(name: str) -> list[str]:
    """门店的几种写法，从长到短：全称、去掉“店”、再去掉城市前缀（上海徐汇店 → 上海徐汇 → 徐汇）。"""
    out = [name]
    if name.endswith("店") and len(name) > 2:
        out.append(name[:-1])
        if len(name) > 4:
            out.append(name[2:-1])
    return out


def _label_drivers(item: dict, drivers: list[dict]) -> None:
    """带动门店第一次出现时补上门店编码（如“上海静安店（SH02）”）：模型按占位符写门店名，常常不带编码，
    也会写简称（“徐汇 -19.8%”）或自带括号（“上海静安店（环比下降 17.9%）”，此时编码并入括号）。
    模型漏写的带动门店（常见于压缩篇幅时）由程序补在“现象”末尾，保证超阈值门店逐一列出。"""
    fields = ("phenomenon", "cause", "suggestion")
    missing = []
    for d in drivers:
        name, label = d.get("store"), d.get("label") or d.get("store")
        if not name or any(label in item[k] for k in fields):
            continue
        code = label[len(name) + 1:-1] if label.startswith(name + "（") else ""
        done = False
        for alias in _store_names(name):
            for k in fields:
                m = re.search(re.escape(alias) + r"(?!店)([（(])?", item[k])
                if not m:
                    continue
                if code:
                    if m.group(1):  # 已有括号：编码并入
                        item[k] = item[k][:m.end()] + code + "，" + item[k][m.end():]
                    else:
                        item[k] = item[k][:m.end()] + f"（{code}）" + item[k][m.end():]
                done = True
                break
            if done:
                break
        if not done:
            missing.append(f"{label}环比 {_pct(d.get('wow'))}")
    if missing:
        p = item["phenomenon"].rstrip()
        item["phenomenon"] = (p + ("" if not p or p[-1] in "。；！" else "。")) + f"带动门店还有：{'、'.join(missing)}。"


def _pct(v: float | None) -> str:
    return fmt_value("x_wow", v)


def _kpi_overall_full(kpis: dict[str, Any]) -> dict[str, Any]:
    """有成本、渠道、目标数据时的核心指标表：全公司及各区域的实收、环比、毛利率、外卖占比、目标达成率。"""
    f = kpis["flat"]

    def row(name: str, prefix: str) -> list[str]:
        return [name, fmt_value("sales", f.get(f"{prefix}.sales")), _pct(f.get(f"{prefix}.sales_wow")),
                fmt_value("gross_margin", f.get(f"{prefix}.gross_margin")),
                fmt_value("delivery_share", f.get(f"{prefix}.delivery_share")),
                fmt_value("target", f.get(f"{prefix}.target")), fmt_value("target_rate", f.get(f"{prefix}.target_rate"))]

    regions = sorted(kpis["tables"]["region"], key=lambda r: r["sales"], reverse=True)
    return {"columns": ["范围", "本周实收", "环比", "毛利率", "外卖占比", "本周目标", "目标达成率"],
            "rows": [row("全公司", "total")] + [row(r["name"], f"region.{r['name']}") for r in regions]}


def _anomaly_table(kpis: dict[str, Any]) -> dict[str, Any]:
    """环比超过阈值的区域与门店全部列出（不受异常条目数上限影响），按影响金额排序。"""
    th = kpis["threshold"]
    rows = []
    for dim, kind in (("region", "区域"), ("store", "门店")):
        for r in kpis["tables"][dim]:
            if r.get("sales_wow") is not None and abs(r["sales_wow"]) > th:
                name = f"{r['name']}（{r['code']}）" if r.get("code") else r["name"]
                rows.append((abs(r["sales"] - r["sales_prev"]),
                             [name, kind if dim == "region" else f"{r.get('region') or ''}{r.get('store_type') or '门店'}",
                              fmt_value("sales", r["sales"]), fmt_value("sales", r["sales_prev"]), _pct(r["sales_wow"]),
                              fmt_value("impact", r["sales"] - r["sales_prev"])]))
    rows.sort(key=lambda x: (x[1][1] != "区域", -x[0]))
    return {"columns": ["对象", "类型", "本周实收", "上周实收", "环比", "变动金额"], "rows": [r for _, r in rows]}


def build_tables(kpis: dict[str, Any]) -> dict[str, dict[str, Any]]:
    f = kpis["flat"]
    overall = [["销售额", fmt_value("sales", f["total.sales"]), fmt_value("sales", f["total.sales_prev"]),
                _pct(f["total.sales_wow"])],
               ["销量（件）", fmt_value("qty", f["total.qty"]), fmt_value("qty", f["total.qty_prev"]),
                _pct(f["total.qty_wow"])]]
    if "total.orders" in f:
        overall += [["订单数", fmt_value("orders", f["total.orders"]), fmt_value("orders", f["total.orders_prev"]),
                     _pct(f["total.orders_wow"])],
                    ["客单价", fmt_value("atv", f["total.atv"]), fmt_value("atv", f["total.atv_prev"]),
                     _pct(f["total.atv_wow"])]]

    def dim_table(rows: list[dict], name_col: str) -> dict[str, Any]:
        rows = sorted(rows, key=lambda r: r["sales"], reverse=True)
        margin = all("gross_margin" in r for r in rows)
        return {"columns": [name_col, "本周销售额", "上周销售额", "环比", "占比"] + (["毛利率"] if margin else []),
                "rows": [[r["name"], fmt_value("sales", r["sales"]), fmt_value("sales", r["sales_prev"]),
                          _pct(r["sales_wow"]), fmt_value("share", r["share"])]
                         + ([fmt_value("gross_margin", r["gross_margin"])] if margin else []) for r in rows]}

    stores = [r for r in kpis["tables"]["store"] if r["sales_wow"] is not None]
    ranked = sorted(stores, key=lambda r: r["sales_wow"], reverse=True)
    n = min(5, len(ranked) // 2) or len(ranked)
    store_rank = {"columns": ["排名", "门店", "区域", "本周销售额", "环比"],
                  "rows": [[f"Top{i}", r["name"], r.get("region") or "", fmt_value("sales", r["sales"]),
                            _pct(r["sales_wow"])] for i, r in enumerate(ranked[:n], 1)]
                  + [[f"Bottom{i}", r["name"], r.get("region") or "", fmt_value("sales", r["sales"]),
                      _pct(r["sales_wow"])] for i, r in enumerate(reversed(ranked[-n:]), 1)]}
    items = {r["name"]: r for r in kpis["tables"].get("item", [])}
    top = [items[n] for n in kpis.get("top_items", []) if n in items]
    return {
        "kpi_overall": _kpi_overall_full(kpis) if "total.gross_margin" in f else
        {"columns": ["指标", "本周", "上周", "环比"], "rows": overall},
        "region_table": dim_table(kpis["tables"]["region"], "区域"),
        "category_table": dim_table(kpis["tables"]["category"], "品类"),
        "store_rank": store_rank,
        "top_items": {"columns": ["排名", "单品", "品类", "本周实收", "环比", "占比"],
                      "rows": [[str(r["rank"]), r["name"], r.get("category") or "", fmt_value("sales", r["sales"]),
                                _pct(r["sales_wow"]), fmt_value("share", r["share"])] for r in top]},
        "anomaly_table": _anomaly_table(kpis),
    }


TABLE_TITLES = {"kpi_overall": "核心指标", "region_table": "区域销售", "category_table": "品类销售",
                "store_rank": "门店环比 Top / Bottom", "top_items": "本周实收前 5 名单品",
                "anomaly_table": "环比波动超过阈值的区域与门店"}


def build_blocks(content: dict[str, Any], kpis: dict[str, Any], charts: dict[str, str],
                 cfg: dict[str, Any], meta: dict[str, Any], minutes: list[dict[str, Any]] | None = None,
                 ) -> tuple[list[Block], dict[str, list[str]], list[str]]:
    """返回 (块列表, 各章节解析后的文字, 无法解析的占位符)。"""
    flat = anomaly_flat(kpis)
    tables = build_tables(kpis)
    blocks: list[Block] = []
    texts: dict[str, list[str]] = {}
    unresolved: list[str] = []

    refs = {m["no"]: m.get("ref", f"第{m['no']}条") for m in minutes or []}
    weeks = {m.get("week") for m in minutes or []} - {None}
    src = f"第 {weeks.pop()} 周周会纪要 " if len(weeks) == 1 else "周会纪要 "

    def r(t: str) -> str:
        out, miss = resolve(" ".join(str(t).split()) if "\n" in str(t) else str(t), flat)
        unresolved.extend(miss)
        return out

    blocks.append(("title", cfg["title"].format_map(_Default(meta))))
    blocks.append(("meta", f"统计周期：{kpis['period_label']}　|　版本：V{meta['version']}　|　生成时间：{meta['generated_at']}"))
    an_info = {a["id"]: a for a in kpis["anomalies"]}
    for sec in cfg["sections"]:
        sid, kind = sec["id"], sec.get("kind", "paragraphs")
        blocks.append(("h2", sec["title"]))
        table_at = len(blocks)  # 异常章节：清单表放在逐条分析之前
        texts[sid] = []
        if kind in ("paragraphs", "bullets"):
            for t in content.get(sid, []):
                rt = r(t)
                texts[sid].append(rt)
                blocks.append(("bullet" if kind == "bullets" else "p", rt))
        elif kind == "anomalies":
            items = content.get("anomalies", [])
            checks: dict[str, list[str]] = {}
            for n in reconcile_notes(content, kpis, minutes or []):
                checks.setdefault(n["id"], []).append(n["text"])
            if not items:
                msg = "本周各项指标波动均在正常范围内（阈值 ±{:.0f}%），无需特别关注的异常。".format(kpis["threshold"] * 100)
                texts[sid].append(msg)
                blocks.append(("p", msg))
            for it in items:
                a = an_info.get(it.get("id"), {})
                head = f"{it.get('id', '')} {a.get('label', it.get('title', ''))}：环比 {_pct(a.get('wow'))}，" \
                       f"变动 {fmt_value('impact', a.get('impact'))}" if a else it.get("title", "")
                ev = it.get("evidence") or []
                basis = f"（依据：{src}{'；'.join(refs.get(e, f'第{e}条') for e in ev)}）" if ev else ""
                item = {"head": head, "phenomenon": r(it.get("phenomenon", "")),
                        "cause": r(it.get("cause", "")) + basis, "suggestion": r(it.get("suggestion", "")),
                        "checks": checks.get(it.get("id"), [])}
                _label_drivers(item, a.get("drivers") or [])
                texts[sid].extend([item["head"], item["phenomenon"], item["cause"], item["suggestion"], *item["checks"]])
                blocks.append(("anomaly", item))
            for n in reconcile_extra(content, kpis, minutes or []):  # 非异常对象的口头说法与数据不一致
                texts[sid].append(n["text"])
                blocks.append(("p", n["text"]))
        elif kind == "appendix":
            for t in appendix_lines(kpis, meta):
                texts[sid].append(t)
                blocks.append(("bullet", t))
        elif kind == "data_quality":
            for t in quality_lines(kpis, meta, minutes or []):
                texts[sid].append(t)
                blocks.append(("bullet", t))
        for tname in sec.get("tables", []):
            if not tables[tname]["rows"]:
                continue
            block = ("table", {"title": TABLE_TITLES.get(tname, tname), **tables[tname]})
            if kind == "anomalies":
                blocks.insert(table_at, block)
                table_at += 1
            else:
                blocks.append(block)
        imgs = [{"path": charts[c], "caption": c} for c in sec.get("charts", []) if c in charts]
        if sec.get("chart_layout") == "row" and len(imgs) > 1:  # 两张图并排，节省篇幅
            blocks.append(("image_row", imgs))
        else:
            blocks += [("image", x) for x in imgs]
    blocks.append(("footer", meta.get("footer", "").format_map(_Default(meta))))
    return blocks, texts, unresolved


CLOSED = re.compile(r"停业|闭店|歇业|装修|改造|暂停营业")
FAULT = re.compile(r"故障|无法上传|未上传|断网|系统异常|宕机")


def _md(d: str) -> str:
    return f"{int(d[5:7])}/{int(d[8:10])}"


def _gap_groups(days: list[dict[str, Any]]) -> list[tuple[str, list[str]]]:
    out: dict[str, list[str]] = {}
    for g in days:
        out.setdefault(g["store"], []).append(g["date"])
    return list(out.items())


def _gap_reason(store: str, minutes: list[dict[str, Any]]) -> tuple[str, dict[str, Any] | None]:
    """缺数日期的原因：在纪要中找提到该门店、且讲停业或系统故障的条目。"""
    from .payloads import store_core
    for m in minutes:
        full = f"{m.get('header') or ''}{m['text']}"
        if store in full or store_core(store) in full:
            if CLOSED.search(full):
                return "closed", m
            if FAULT.search(full):
                return "fault", m
    return "unknown", None


def handling_lines(meta: dict[str, Any]) -> list[str]:
    """对输入材料做过的处理（更正版替代、多份合并、隐私剔除 / 打码），数据质量说明与附录共用。"""
    lines = []
    if meta.get("superseded_files"):
        lines.append(f"被后上传的更正版替代、未使用的文件：{'、'.join(meta['superseded_files'])}。")
    if meta.get("overridden_rows"):
        lines.append(f"多份数据合并：同一记录在多份文件中重复出现的 {meta['overridden_rows']} 行，以后上传的文件为准。")
    if meta.get("redacted_columns"):
        lines.append(f"隐私保护：销售明细中的“{'”“'.join(meta['redacted_columns'])}”含个人信息（手机号 / 身份证号），"
                     "读取时已剔除或打码，不参与计算、不进入报告。")
    if meta.get("minutes_masked"):
        lines.append(f"隐私保护：周会纪要中 {meta['minutes_masked']} 处含手机号 / 身份证号的内容已打码后再交给模型处理。")
    return lines


def quality_lines(kpis: dict[str, Any], meta: dict[str, Any], minutes: list[dict[str, Any]]) -> list[str]:
    """数据质量说明（程序生成，数字精确）：退货、重复、缺数日期及原因、上周缺数对环比的影响、其他清洗处理。"""
    q = kpis.get("quality") or {}
    weeks = {kpis["period"]: "本周", kpis["prev_period"]: "上周"}
    from .periods import parse_week
    span = {k: (parse_week(k).start.isoformat(), parse_week(k).end.isoformat()) for k in weeks}

    def which(d: str | None) -> str | None:
        return next((label for k, label in weeks.items() if d and span[k][0] <= d <= span[k][1]), None)

    lines = []
    if meta.get("minutes_missing"):
        lines.append("周会纪要：本次未上传周会纪要，异常原因无法引用纪要，均标注“原因待核实”；"
                     "如需引用纪要说明原因，请上传对应周的纪要后重新发起任务。")
    ret = q.get("returns") or {}
    if ret.get("count"):
        t = f"退货：本周退货 {ret['count']} 笔，合计 {ret['amount']:,.0f} 元，已按负数直接冲减实收"
        if ret.get("top"):
            x = ret["top"][0]
            t += f"；金额最大的是{x['store']} {_md(x['date'])} {x['item']}退货 {x['qty']:.0f} 份（{x['amount']:,.0f} 元）"
        lines.append(t + "。")
    else:
        lines.append("退货：本周没有退货记录。")
    issues = (meta.get("cleaning") or {}).get("issues", [])
    for it in issues:
        if it.get("kind") == "duplicate" and which(it.get("date")):
            lines.append(f"重复记录：{which(it['date'])}{it['row'].split(' ', 1)[-1]} {_md(it['date'])} "
                         f"{it['problem']}，已{it['action']}，再计算各项指标。")
    for store, days in _gap_groups(q.get("missing_days", [])):
        when = "、".join(_md(d) for d in days) if len(days) < 3 else f"{_md(days[0])}–{_md(days[-1])}"
        kind, m = _gap_reason(store, minutes)
        cite = f"据{_src_of(m)}（{m.get('ref')}）" if m else ""
        if kind == "closed":
            why = next((c.strip() for c in re.split(r"[，。；;]", m["text"]) if CLOSED.search(c)), "该店停业")
            lines.append(f"缺数日期：{store} {when} 无销售数据，{cite}：“{why}”，属正常停业，不是数据缺失。")
        elif kind == "fault":
            manual = re.search(r"约\s*([\d,，.]+)\s*元", m["text"])
            extra = (f"纪要提到的手工台账实收约 {manual.group(1)} 元未经系统核实，未计入实收合计" if manual
                     else "当日实收未计入")
            lines.append(f"缺数日期：{store} {when} 无系统销售数据，{cite}系收银系统故障；{extra}，"
                         "该店及所在区域本周实收与环比偏低。")
        else:
            lines.append(f"缺数日期：{store} {when} 无销售数据，纪要未提及原因，原因待核实；当日按无销售计算。")
    for store, days in _gap_groups(q.get("missing_days_prev", [])):
        wow = kpis["flat"].get(f"store.{store}.sales_wow")
        lines.append(f"环比基数：上周{store} {'、'.join(_md(d) for d in days)} 无销售数据，上周基数偏低，"
                     f"本周该店环比（{_pct(wow)}）偏高，解读时请注意。")
    other = [it for it in issues if it.get("kind") in ("null", "negative", "qty") and which(it.get("date"))]
    if other:
        lines.append(f"其他清洗：本周及上周共 {len(other)} 行数据存在空值、非退货负数或销量异常，已按规则处理（"
                     + "；".join(f"{it['row']}，{it['problem']}，{it['action']}" for it in other[:3])
                     + ("等" if len(other) > 3 else "") + "）。")
    dup_seen = any(it.get("kind") == "duplicate" and which(it.get("date")) for it in issues)
    if not dup_seen:
        lines.append("重复记录：本周及上周未发现重复上传的记录。")
    if not q.get("missing_days") and not q.get("missing_days_prev"):
        lines.append("缺数日期：本周及上周各门店营业日均有销售数据。")
    lines += handling_lines(meta)
    lines.append(f"口径：实收 = 原价 − 折扣，退货为负数冲减；毛利率 = （实收 − 成本）÷ 实收；ISO 周（周一至周日），"
                 f"环比基期 {kpis['prev_period']}；数据来源：{'、'.join(meta.get('sources', []))}"
                 f"{_master_note(meta.get('master_origin') or {})}。")
    return lines


def _master_note(origin: dict[str, str]) -> str:
    """主数据各工作表的来源，如“；主数据：门店信息、商品信息取自员工配置自带的主数据，销售目标取自上传的主数据文件”。"""
    by: dict[str, list[str]] = {}
    for sheet, src in origin.items():
        by.setdefault(src, []).append(sheet)
    return "；主数据：" + "，".join(f"{'、'.join(v)}取自{k}" if k != "未提供" else f"{'、'.join(v)}未提供"
                                for k, v in by.items()) if by else ""


def _src_of(m: dict[str, Any]) -> str:
    return f"第 {m['week']} 周周会纪要" if m.get("week") else "周会纪要"


def appendix_lines(kpis: dict[str, Any], meta: dict[str, Any]) -> list[str]:
    c = meta.get("cleaning", {})
    lines = [
        f"统计口径：ISO 周（周一至周日）；本周 {kpis['period']}，环比基期 {kpis['prev_period']}。",
        f"异常判定：区域 / 门店 / 品类销售额环比超过 ±{kpis['threshold'] * 100:.0f}% 视为异常，按影响金额排序；"
        "区域×品类仅在偏离所在区域整体趋势时单独列出。",
        f"数据来源：{'、'.join(meta.get('sources', []))}。",
        f"数据清洗：原始 {c.get('total_rows', '-')} 行，剔除重复 {c.get('duplicates', 0)} 行、"
        f"关键字段为空 {c.get('null_rows', 0)} 行、销售额为负 {c.get('negative_rows', 0)} 行，"
        f"销量异常 {c.get('bad_qty_rows', 0)} 行（保留销售额、不计销量），保留 {c.get('kept_rows', '-')} 行。",
        "报告中的全部数字由程序根据销售明细计算生成，文字分析中的原因均引用周会纪要，纪要未提及的标注“原因待核实”。",
    ]
    issues = c.get("issues", [])
    for it in issues[:8]:
        lines.append(f"清洗明细：{it['row']}，{it['problem']}，{it['action']}。")
    if len(issues) > 8:
        lines.append(f"清洗明细：另有 {len(issues) - 8} 处，见质检记录附件。")
    if c.get("zero_sales_days"):
        lines.append("销售额为 0 的记录（已保留，多为停业）：" + "；".join(
            f"{store} {'、'.join(days)}" for store, days in c["zero_sales_days"].items()) + "。")
    lines += handling_lines(meta)
    if meta.get("alias_mapping"):
        lines.append("列名识别：" + "，".join(f"“{k}”按“{v}”处理" for k, v in meta["alias_mapping"].items()) + "。")
    return lines


# ---------------------------------------------------------------- writers

def _set_cn_font(run, size: float | None = None, bold: bool = False, color: RGBColor | None = None) -> None:
    run.font.name = "Microsoft YaHei"
    run._element.get_or_add_rPr().get_or_add_rFonts().set(qn("w:eastAsia"), "微软雅黑")
    if size:
        run.font.size = Pt(size)
    run.bold = bold
    if color is not None:
        run.font.color.rgb = color


def write_docx(blocks: list[Block], path: Path) -> None:
    doc = Document()
    sec = doc.sections[0]
    sec.left_margin = sec.right_margin = Cm(2.2)
    sec.top_margin = sec.bottom_margin = Cm(2.0)
    normal = doc.styles["Normal"]
    normal.font.name = "Microsoft YaHei"
    normal.element.get_or_add_rPr().get_or_add_rFonts().set(qn("w:eastAsia"), "微软雅黑")
    normal.font.size = Pt(10.5)
    normal.paragraph_format.space_after = Pt(3)  # Word 默认段后 10 磅、1.15 倍行距，周报篇幅有限，收紧
    normal.paragraph_format.line_spacing = 1.0
    doc.styles["List Bullet"].paragraph_format.space_after = Pt(2)
    for kind, val in blocks:
        if kind == "title":
            p = doc.add_paragraph()
            _set_cn_font(p.add_run(val), 18, True, RGBColor(0x1F, 0x3A, 0x5F))
        elif kind == "meta":
            p = doc.add_paragraph()
            _set_cn_font(p.add_run(val), 9, color=RGBColor(0x66, 0x66, 0x66))
        elif kind == "h2":
            p = doc.add_paragraph()
            p.paragraph_format.space_before = Pt(8)
            _set_cn_font(p.add_run(val), 13, True, RGBColor(0x1F, 0x3A, 0x5F))
        elif kind == "p":
            doc.add_paragraph(val)
        elif kind == "bullet":
            doc.add_paragraph(val, style="List Bullet")
        elif kind == "anomaly":
            p = doc.add_paragraph()
            _set_cn_font(p.add_run(val["head"]), 10.5, True)
            for label, key in (("现象", "phenomenon"), ("原因", "cause"), ("建议", "suggestion")):
                if val[key]:
                    q = doc.add_paragraph()
                    q.paragraph_format.left_indent = Cm(0.6)
                    _set_cn_font(q.add_run(f"{label}："), bold=True)
                    q.add_run(val[key])
            for note in val.get("checks", []):
                q = doc.add_paragraph()
                q.paragraph_format.left_indent = Cm(0.6)
                label, _, body = note.partition("：")
                _set_cn_font(q.add_run(f"{label}："), bold=True, color=RGBColor(0xB0, 0x5A, 0x00))
                _set_cn_font(q.add_run(body), color=RGBColor(0x5A, 0x3A, 0x00))
        elif kind == "table":
            cap = doc.add_paragraph()
            _set_cn_font(cap.add_run(val["title"]), 9.5, True, RGBColor(0x44, 0x44, 0x44))
            t = doc.add_table(rows=1, cols=len(val["columns"]))
            t.style = "Light Grid Accent 1"
            for i, c in enumerate(val["columns"]):
                t.rows[0].cells[i].text = c
            for row in val["rows"]:
                cells = t.add_row().cells
                for i, c in enumerate(row):
                    cells[i].text = str(c)
                    if i > 0:
                        cells[i].paragraphs[0].alignment = WD_ALIGN_PARAGRAPH.RIGHT
            for row in t.rows:
                for cell in row.cells:
                    for r in cell.paragraphs[0].runs:
                        r.font.size = Pt(9)
        elif kind == "image":
            doc.add_picture(val["path"], width=Cm(15.5))
        elif kind == "image_row":
            t = doc.add_table(rows=1, cols=len(val))
            for cell, img in zip(t.rows[0].cells, val):
                cell.paragraphs[0].add_run().add_picture(img["path"], width=Cm(16.0 / len(val) - 0.2))
        elif kind == "footer" and val:
            p = doc.add_paragraph()
            _set_cn_font(p.add_run(val), 8.5, color=RGBColor(0x88, 0x88, 0x88))
    doc.save(path)


def write_md(blocks: list[Block], path: Path) -> None:
    out: list[str] = []
    for kind, val in blocks:
        if kind == "title":
            out += [f"# {val}", ""]
        elif kind == "meta":
            out += [f"> {val}", ""]
        elif kind == "h2":
            out += [f"## {val}", ""]
        elif kind == "p":
            out += [val, ""]
        elif kind == "bullet":
            out.append(f"- {val}")
        elif kind == "anomaly":
            out += [f"**{val['head']}**", ""]
            out += [f"- {lab}：{val[k]}" for lab, k in (("现象", "phenomenon"), ("原因", "cause"), ("建议", "suggestion")) if val[k]]
            out += [f"- **{n.partition('：')[0]}**：{n.partition('：')[2]}" for n in val.get("checks", [])]
            out.append("")
        elif kind == "table":
            out += ["", f"*{val['title']}*", "", "| " + " | ".join(val["columns"]) + " |",
                    "|" + "---|" * len(val["columns"])]
            out += ["| " + " | ".join(str(c) for c in row) + " |" for row in val["rows"]]
            out.append("")
        elif kind == "image":
            out += ["", f"![{val['caption']}]({Path(val['path']).name})", ""]
        elif kind == "image_row":
            out += [""] + [f"![{x['caption']}]({Path(x['path']).name})" for x in val] + [""]
        elif kind == "footer" and val:
            out += ["", "---", f"*{val}*"]
    path.write_text("\n".join(out) + "\n", encoding="utf-8")


_HTML_CSS = """
body{font-family:"Microsoft YaHei","PingFang SC","Source Han Sans CN",sans-serif;max-width:860px;margin:32px auto;
padding:0 20px;color:#222;line-height:1.75;font-size:15px}
h1{color:#1f3a5f;font-size:26px;margin-bottom:4px}h2{color:#1f3a5f;font-size:19px;margin-top:28px;
border-bottom:1px solid #dde3ea;padding-bottom:4px}.meta{color:#666;font-size:13px}.cap{font-weight:bold;color:#444;
margin:14px 0 4px;font-size:13px}table{border-collapse:collapse;width:100%;font-size:13px;margin-bottom:8px}
th,td{border:1px solid #d5dce4;padding:4px 8px}th{background:#eef3f8;text-align:left}td.n{text-align:right}
img{max-width:100%;margin:8px 0}.anomaly{margin:10px 0 14px}.anomaly p{margin:2px 0 2px 1.2em}
.anomaly .head{margin-left:0;font-weight:bold}.check{color:#5a3a00}.check b{color:#b05a00}
.footer{color:#888;font-size:12px;border-top:1px solid #eee;margin-top:28px;padding-top:8px}
"""


def write_html(blocks: list[Block], path: Path) -> None:
    """单文件 HTML：图表以 base64 内嵌，发邮件或用浏览器打开即可，不依赖其他文件。"""
    import base64
    from html import escape as e

    title = next((v for k, v in blocks if k == "title"), "经营周报")
    out = ["<!doctype html>", '<html lang="zh-CN"><head><meta charset="utf-8">',
           f"<title>{e(title)}</title><style>{_HTML_CSS}</style></head><body>"]
    in_list = False
    for kind, val in blocks:
        if in_list and kind != "bullet":
            out.append("</ul>")
            in_list = False
        if kind == "title":
            out.append(f"<h1>{e(val)}</h1>")
        elif kind == "meta":
            out.append(f'<p class="meta">{e(val)}</p>')
        elif kind == "h2":
            out.append(f"<h2>{e(val)}</h2>")
        elif kind == "p":
            out.append(f"<p>{e(val)}</p>")
        elif kind == "bullet":
            if not in_list:
                out.append("<ul>")
                in_list = True
            out.append(f"<li>{e(val)}</li>")
        elif kind == "anomaly":
            out.append(f'<div class="anomaly"><p class="head">{e(val["head"])}</p>')
            out += [f"<p><b>{lab}：</b>{e(val[k])}</p>" for lab, k in
                    (("现象", "phenomenon"), ("原因", "cause"), ("建议", "suggestion")) if val[k]]
            out += [f'<p class="check"><b>{e(n.partition("：")[0])}：</b>{e(n.partition("：")[2])}</p>'
                    for n in val.get("checks", [])]
            out.append("</div>")
        elif kind == "table":
            out.append(f'<p class="cap">{e(val["title"])}</p><table><thead><tr>'
                       + "".join(f"<th>{e(c)}</th>" for c in val["columns"]) + "</tr></thead><tbody>")
            out += ["<tr>" + "".join(f'<td{" class=n" if i else ""}>{e(str(c))}</td>' for i, c in enumerate(row))
                    + "</tr>" for row in val["rows"]]
            out.append("</tbody></table>")
        elif kind in ("image", "image_row"):
            imgs = val if kind == "image_row" else [val]
            style = f' style="width:{100 / len(imgs) - 1:.0f}%"' if len(imgs) > 1 else ""
            for x in imgs:
                data = base64.b64encode(Path(x["path"]).read_bytes()).decode()
                out.append(f'<img alt="{e(x["caption"])}"{style} src="data:image/png;base64,{data}">')
        elif kind == "footer" and val:
            out.append(f'<p class="footer">{e(val)}</p>')
    if in_list:
        out.append("</ul>")
    out.append("</body></html>")
    path.write_text("\n".join(out) + "\n", encoding="utf-8")


LINE = 1.78  # 行高 / 字号：按 Word 中微软雅黑单倍行距实测（10.5 磅字行距约 18 磅），页数估算与 Word 一致
SAFETY = 0.15  # 英寸：每页底部额外预留。按 46 份报告（deepseek / gemini 真实模型 39 份、Mock 7 份）与 Word 实际页数对比：
               # 不预留时有 1 份把 4 页估成 3 页（Word 把最后一段挤到下一页）；预留 0.1～0.2 时无估少、1 份估多；0.25 以上估多增加


class _PdfLayout:
    """用 matplotlib 排版 A4 PDF：内置思源黑体，中文不依赖系统字体，也不需要 Word / LibreOffice。"""
    W, H, LEFT, RIGHT, TOP, BOTTOM = 8.27, 11.69, 0.85, 0.85, 0.8, 0.8  # 英寸

    def __init__(self, pdf: Any, reserve: float = 0.0):
        import matplotlib.pyplot as plt

        from .tools import charts  # noqa: F401  注册中文字体

        self.bottom = self.BOTTOM + reserve  # 估算页数时每页底部多预留的空间
        self.plt, self.pdf, self.fig, self.page = plt, pdf, None, 0
        self.cw = self.W - self.LEFT - self.RIGHT
        self.new_page()

    def new_page(self) -> None:
        if self.fig is not None:
            self.pdf.savefig(self.fig)
            self.plt.close(self.fig)
        self.fig = self.plt.figure(figsize=(self.W, self.H))
        self.page += 1
        self.fig.text(0.5, 0.4 / self.H, f"第 {self.page} 页", ha="center", fontsize=8, color="#999")
        self.y = self.H - self.TOP

    def close(self) -> None:
        self.pdf.savefig(self.fig)
        self.plt.close(self.fig)

    def need(self, h: float) -> None:
        if self.y - h < self.bottom:
            self.new_page()

    @staticmethod
    def width(s: str, size: float) -> float:
        import unicodedata
        return sum(size * (1.0 if unicodedata.east_asian_width(ch) in "WF" else 0.56) for ch in s) / 72

    def wrap(self, s: str, size: float, max_w: float) -> list[str]:
        lines, cur = [], ""
        for ch in s:
            if ch == "\n" or (cur and self.width(cur + ch, size) > max_w):
                lines.append(cur)
                cur = "" if ch == "\n" else ch
            else:
                cur += ch
        return lines + [cur] if cur or not lines else lines

    def text(self, s: str, size: float = 10.5, color: str = "#222222", indent: float = 0.0, prefix: str = "",
             before: float = 0.0, after: float = 4.0) -> None:
        self.y -= before / 72
        pw = self.width(prefix, size)
        lh = size * LINE / 72
        for i, ln in enumerate(self.wrap(s, size, self.cw - indent - pw)):
            self.need(lh)
            if i == 0 and prefix:
                self.fig.text((self.LEFT + indent) / self.W, (self.y - size / 72) / self.H, prefix, fontsize=size,
                              color=color)
            self.fig.text((self.LEFT + indent + pw) / self.W, (self.y - size / 72) / self.H, ln, fontsize=size,
                          color=color)
            self.y -= lh
        self.y -= after / 72

    def table(self, title: str, columns: list[str], rows: list[list[Any]], size: float = 9.0) -> None:
        from matplotlib.patches import Rectangle

        self.text(title, 9.5, "#444444", before=4, after=2)
        cells = [columns] + [[str(c) for c in r] for r in rows]
        natural = [max(self.width(str(r[i]), size) for r in cells) + 0.16 for i in range(len(columns))]
        scale = min(1.0, self.cw / sum(natural))
        widths = [w * scale for w in natural] if scale < 1 else [w + (self.cw - sum(natural)) / len(natural)
                                                                  for w in natural]
        rh = size * 2.1 / 72  # 与 Word 表格行高（9 磅字约 18 磅）一致

        def row(vals: list[str], header: bool) -> None:
            self.need(rh)
            x = self.LEFT
            if header:
                self.fig.patches.append(Rectangle((self.LEFT / self.W, (self.y - rh) / self.H), self.cw / self.W,
                                                  rh / self.H, transform=self.fig.transFigure, color="#eef3f8"))
            for i, (v, w) in enumerate(zip(vals, widths)):
                right = i > 0  # 数字列右对齐，表头与数据对齐方式一致
                self.fig.text((x + (w - 0.08 if right else 0.08)) / self.W, (self.y - rh * 0.68) / self.H, v,
                              fontsize=size, ha="right" if right else "left", color="#222222")
                x += w
            self.fig.add_artist(self.plt.Line2D([self.LEFT / self.W, (self.LEFT + self.cw) / self.W],
                                                [(self.y - rh) / self.H] * 2, color="#d5dce4", linewidth=0.6,
                                                transform=self.fig.transFigure))
            self.y -= rh

        self.need(rh * 2)  # 表头至少带一行数据，不单独留在页尾
        page = self.page
        row(columns, True)
        for r in cells[1:]:
            if self.y - rh < self.bottom:
                self.new_page()
            if self.page != page:  # 表格跨页时在新页重复表头
                page = self.page
                row(columns, True)
            row(r, False)
        self.y -= 6 / 72

    def image_row(self, paths: list[str]) -> None:
        import matplotlib.image as mpimg

        gap = 0.15
        w = (self.cw - gap * (len(paths) - 1)) / len(paths)
        imgs = [mpimg.imread(p) for p in paths]
        h = max(w * im.shape[0] / im.shape[1] for im in imgs)
        self.need(h)
        for i, im in enumerate(imgs):
            ax = self.fig.add_axes([(self.LEFT + i * (w + gap)) / self.W, (self.y - h) / self.H, w / self.W, h / self.H])
            ax.imshow(im)
            ax.axis("off")
        self.y -= h + 6 / 72

    def image(self, path: str) -> None:
        import matplotlib.image as mpimg

        img = mpimg.imread(path)
        h = self.cw * img.shape[0] / img.shape[1]
        self.need(h)
        ax = self.fig.add_axes([self.LEFT / self.W, (self.y - h) / self.H, self.cw / self.W, h / self.H])
        ax.imshow(img)
        ax.axis("off")
        self.y -= h + 6 / 72


class _NoPdf:
    def savefig(self, fig: Any) -> None:
        pass


def count_pages(blocks: list[Block]) -> int:
    """按 A4 版式排一遍（不写文件），估算页数，供“篇幅”质检使用。页脚说明不计入正文。"""
    lay = _PdfLayout(_NoPdf(), reserve=SAFETY)
    _layout(lay, [b for b in blocks if b[0] != "footer"])
    lay.close()
    return lay.page


def write_pdf(blocks: list[Block], path: Path) -> None:
    from matplotlib.backends.backend_pdf import PdfPages

    with PdfPages(path, metadata={"CreationDate": None}) as pdf:  # 去掉生成时间，同样内容生成同样的文件
        lay = _PdfLayout(pdf)
        _layout(lay, blocks)
        lay.close()


def _layout(lay: "_PdfLayout", blocks: list[Block]) -> None:
    for kind, val in blocks:
        if kind == "title":
            lay.text(val, 18, "#1F3A5F", after=4)
        elif kind == "meta":
            lay.text(val, 8.5, "#666666", after=8)
        elif kind == "h2":
            lay.need(0.6)  # 避免标题落在页尾
            lay.text(val, 13, "#1F3A5F", before=8, after=4)
        elif kind == "p":
            lay.text(val)
        elif kind == "bullet":
            lay.text(val, prefix="• ", indent=0.1, after=2)
        elif kind == "anomaly":
            lay.text(val["head"], 10.5, "#1F3A5F", before=4, after=2)
            for label, key in (("现象", "phenomenon"), ("原因", "cause"), ("建议", "suggestion")):
                if val[key]:
                    lay.text(val[key], prefix=f"{label}：", indent=0.25, after=2)
            for note in val.get("checks", []):
                label, _, body = note.partition("：")
                lay.text(body, color="#5A3A00", prefix=f"{label}：", indent=0.25, after=2)
        elif kind == "table":
            lay.table(val["title"], val["columns"], val["rows"])
        elif kind == "image":
            lay.image(val["path"])
        elif kind == "image_row":
            lay.image_row([x["path"] for x in val])
        elif kind == "footer" and val:
            lay.text(val, 8, "#888888", before=10)


WRITERS = {"docx": write_docx, "md": write_md, "html": write_html, "pdf": write_pdf}
