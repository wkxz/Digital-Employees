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
        elif kind == "image":
            data = base64.b64encode(Path(val["path"]).read_bytes()).decode()
            out.append(f'<img alt="{e(val["caption"])}" src="data:image/png;base64,{data}">')
        elif kind == "footer" and val:
            out.append(f'<p class="footer">{e(val)}</p>')
    if in_list:
        out.append("</ul>")
    out.append("</body></html>")
    path.write_text("\n".join(out) + "\n", encoding="utf-8")


class _PdfLayout:
    """用 matplotlib 排版 A4 PDF：内置思源黑体，中文不依赖系统字体，也不需要 Word / LibreOffice。"""
    W, H, LEFT, RIGHT, TOP, BOTTOM = 8.27, 11.69, 0.85, 0.85, 0.8, 0.8  # 英寸

    def __init__(self, pdf: Any):
        import matplotlib.pyplot as plt

        from .tools import charts  # noqa: F401  注册中文字体

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
        if self.y - h < self.BOTTOM:
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
        lh = size * 1.6 / 72
        for i, ln in enumerate(self.wrap(s, size, self.cw - indent - pw)):
            self.need(lh)
            if i == 0 and prefix:
                self.fig.text((self.LEFT + indent) / self.W, (self.y - size / 72) / self.H, prefix, fontsize=size,
                              color=color)
            self.fig.text((self.LEFT + indent + pw) / self.W, (self.y - size / 72) / self.H, ln, fontsize=size,
                          color=color)
            self.y -= lh
        self.y -= after / 72

    def table(self, title: str, columns: list[str], rows: list[list[Any]], size: float = 8.5) -> None:
        from matplotlib.patches import Rectangle

        self.text(title, 9.5, "#444444", before=4, after=2)
        cells = [columns] + [[str(c) for c in r] for r in rows]
        natural = [max(self.width(str(r[i]), size) for r in cells) + 0.16 for i in range(len(columns))]
        scale = min(1.0, self.cw / sum(natural))
        widths = [w * scale for w in natural] if scale < 1 else [w + (self.cw - sum(natural)) / len(natural)
                                                                  for w in natural]
        rh = size * 1.9 / 72

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
            if self.y - rh < self.BOTTOM:
                self.new_page()
            if self.page != page:  # 表格跨页时在新页重复表头
                page = self.page
                row(columns, True)
            row(r, False)
        self.y -= 6 / 72

    def image(self, path: str) -> None:
        import matplotlib.image as mpimg

        img = mpimg.imread(path)
        h = self.cw * img.shape[0] / img.shape[1]
        self.need(h)
        ax = self.fig.add_axes([self.LEFT / self.W, (self.y - h) / self.H, self.cw / self.W, h / self.H])
        ax.imshow(img)
        ax.axis("off")
        self.y -= h + 6 / 72


def write_pdf(blocks: list[Block], path: Path) -> None:
    from matplotlib.backends.backend_pdf import PdfPages

    with PdfPages(path, metadata={"CreationDate": None}) as pdf:  # 去掉生成时间，同样内容生成同样的文件
        lay = _PdfLayout(pdf)
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
            elif kind == "footer" and val:
                lay.text(val, 8, "#888888", before=10)
        lay.close()


WRITERS = {"docx": write_docx, "md": write_md, "html": write_html, "pdf": write_pdf}
