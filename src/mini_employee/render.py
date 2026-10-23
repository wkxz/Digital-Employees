"""把“模型写的文字（含占位符）+ 程序算的表格/图表”组装成交付文件（docx / md）。

先组装成与格式无关的块列表，再分别写成 Word 和 Markdown，新增交付格式只需加一个 writer。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml.ns import qn
from docx.shared import Cm, Pt, RGBColor

from .placeholders import fmt_value, resolve
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


def _pct(v: float | None) -> str:
    return fmt_value("x_wow", v)


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
        return {"columns": [name_col, "本周销售额", "上周销售额", "环比", "占比"],
                "rows": [[r["name"], fmt_value("sales", r["sales"]), fmt_value("sales", r["sales_prev"]),
                          _pct(r["sales_wow"]), fmt_value("share", r["share"])] for r in rows]}

    stores = [r for r in kpis["tables"]["store"] if r["sales_wow"] is not None]
    ranked = sorted(stores, key=lambda r: r["sales_wow"], reverse=True)
    n = min(5, len(ranked) // 2) or len(ranked)
    store_rank = {"columns": ["排名", "门店", "区域", "本周销售额", "环比"],
                  "rows": [[f"Top{i}", r["name"], r.get("region") or "", fmt_value("sales", r["sales"]),
                            _pct(r["sales_wow"])] for i, r in enumerate(ranked[:n], 1)]
                  + [[f"Bottom{i}", r["name"], r.get("region") or "", fmt_value("sales", r["sales"]),
                      _pct(r["sales_wow"])] for i, r in enumerate(reversed(ranked[-n:]), 1)]}
    return {
        "kpi_overall": {"columns": ["指标", "本周", "上周", "环比"], "rows": overall},
        "region_table": dim_table(kpis["tables"]["region"], "区域"),
        "category_table": dim_table(kpis["tables"]["category"], "品类"),
        "store_rank": store_rank,
    }


TABLE_TITLES = {"kpi_overall": "核心指标", "region_table": "区域销售", "category_table": "品类销售",
                "store_rank": "门店环比 Top / Bottom"}


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
                head = f"{it.get('id', '')} {a.get('label', it.get('title', ''))}：销售额环比 {_pct(a.get('wow'))}，" \
                       f"影响 {fmt_value('impact', a.get('impact'))}" if a else it.get("title", "")
                ev = it.get("evidence") or []
                basis = f"（依据：周会纪要 {'；'.join(refs.get(e, f'第{e}条') for e in ev)}）" if ev else ""
                item = {"head": head, "phenomenon": r(it.get("phenomenon", "")),
                        "cause": r(it.get("cause", "")) + basis, "suggestion": r(it.get("suggestion", "")),
                        "checks": checks.get(it.get("id"), [])}
                texts[sid].extend([item["head"], item["phenomenon"], item["cause"], item["suggestion"], *item["checks"]])
                blocks.append(("anomaly", item))
        elif kind == "appendix":
            for t in appendix_lines(kpis, meta):
                texts[sid].append(t)
                blocks.append(("bullet", t))
        for tname in sec.get("tables", []):
            blocks.append(("table", {"title": TABLE_TITLES.get(tname, tname), **tables[tname]}))
        for cname in sec.get("charts", []):
            if cname in charts:
                blocks.append(("image", {"path": charts[cname], "caption": cname}))
    blocks.append(("footer", meta.get("footer", "").format_map(_Default(meta))))
    return blocks, texts, unresolved


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
        "报告中的全部数字由程序根据销售明细计算生成，文字分析中的原因均引用周会纪要，纪要未提及的标注“原因待确认”。",
    ]
    issues = c.get("issues", [])
    for it in issues[:8]:
        lines.append(f"清洗明细：{it['row']}，{it['problem']}，{it['action']}。")
    if len(issues) > 8:
        lines.append(f"清洗明细：另有 {len(issues) - 8} 处，见质检记录附件。")
    if c.get("zero_sales_days"):
        lines.append("销售额为 0 的记录（已保留，多为停业）：" + "；".join(
            f"{store} {'、'.join(days)}" for store, days in c["zero_sales_days"].items()) + "。")
    if meta.get("superseded_files"):
        lines.append(f"被后上传的更正版替代、未使用的文件：{'、'.join(meta['superseded_files'])}。")
    if meta.get("overridden_rows"):
        lines.append(f"多份数据合并：同一日期、门店、品类在多份文件中重复出现的 {meta['overridden_rows']} 行，以后上传的文件为准。")
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
    normal = doc.styles["Normal"]
    normal.font.name = "Microsoft YaHei"
    normal.element.get_or_add_rPr().get_or_add_rFonts().set(qn("w:eastAsia"), "微软雅黑")
    normal.font.size = Pt(10.5)
    for kind, val in blocks:
        if kind == "title":
            p = doc.add_paragraph()
            _set_cn_font(p.add_run(val), 18, True, RGBColor(0x1F, 0x3A, 0x5F))
        elif kind == "meta":
            p = doc.add_paragraph()
            _set_cn_font(p.add_run(val), 9, color=RGBColor(0x66, 0x66, 0x66))
        elif kind == "h2":
            p = doc.add_paragraph()
            p.paragraph_format.space_before = Pt(12)
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
        elif kind == "image":
            doc.add_picture(val["path"], width=Cm(15.5))
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
        elif kind == "footer" and val:
            out += ["", "---", f"*{val}*"]
    path.write_text("\n".join(out) + "\n", encoding="utf-8")


WRITERS = {"docx": write_docx, "md": write_md}
