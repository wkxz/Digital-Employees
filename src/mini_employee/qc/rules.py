"""质检规则。每条规则是一个函数，按员工配置中的 quality_checks 加载并传入参数。

新增规则：在本文件写一个函数并用 @rule("名称") 注册，然后在 employee.yaml 的 quality_checks 中引用。
不写代码也能加的规则：keyword_required（必须提到某些词）、regex_forbidden（不得出现某种写法）。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable

from ..placeholders import PH_RE, find_keys, fmt_value
from ..tools.data import IDCARD_RE, PHONE_RE


@dataclass
class QCContext:
    content: dict[str, Any]
    texts: dict[str, list[str]]
    unresolved: list[str]
    images: list[str]
    tables: list[str]
    kpis: dict[str, Any]
    flat: dict[str, Any]
    minutes: list[dict[str, Any]]
    sections: list[dict[str, Any]]
    todos: list[dict[str, Any]] = field(default_factory=list)
    prev_content: dict[str, Any] | None = None
    pages: int | None = None  # 按交付版式估算的页数


@dataclass
class Result:
    rule: str
    name: str
    passed: bool
    severity: str
    details: list[Any] = field(default_factory=list)
    fix_hint: str = ""

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


RuleFn = Callable[[QCContext, dict[str, Any]], tuple[bool, list[Any], str]]
RULES: dict[str, tuple[str, RuleFn]] = {}


def rule(rid: str, name: str):
    def deco(fn: RuleFn) -> RuleFn:
        RULES[rid] = (name, fn)
        return fn
    return deco


def _text_sections(ctx: QCContext) -> list[str]:
    return [s["id"] for s in ctx.sections if s.get("kind", "paragraphs") in ("paragraphs", "bullets")]


def _iter_raw(ctx: QCContext):
    """遍历模型写的全部文字：(章节, 序号, 字段, 文本)。字段仅异常条目有意义。"""
    for sid in _text_sections(ctx):
        for i, t in enumerate(ctx.content.get(sid, [])):
            yield sid, i, None, t
    for i, it in enumerate(ctx.content.get("anomalies", [])):
        for k in ("phenomenon", "cause", "suggestion"):
            yield "anomalies", i, k, it.get(k, "")


def _squash(s: str) -> str:
    return re.sub(r"\s", "", s)


def _clause_in_minutes(ctx: QCContext, text: str, start: int, end: int) -> bool:
    """数字所在的分句原样出现在纪要中（如会议决定“额度为50万元”），视为引用纪要，不是模型算的数字。"""
    left = max(text.rfind(ch, 0, start) for ch in "，,；;。：:“”") + 1
    rights = [i for i in (text.find(ch, end) for ch in "，,；;。“”") if i != -1]
    clause = _squash(text[left:min(rights) if rights else len(text)])
    return len(clause) >= 6 and any(clause in _squash(m["text"]) for m in ctx.minutes)


def _quoted_from_minutes(ctx: QCContext, item_index: int, number: str) -> bool:
    """异常原因可以原样引用纪要中的数字（如“客流下降近40%”），前提是该数字确实出现在它引用的纪要条目里。"""
    items = ctx.content.get("anomalies", [])
    if item_index >= len(items):
        return False
    cited = {m["no"]: m for m in ctx.minutes}
    return any(_squash(number) in _squash(cited[e]["text"]) for e in items[item_index].get("evidence") or [] if e in cited)


# ---------------------------------------------------------------- 结构完整

@rule("structure", "结构完整")
def structure(ctx: QCContext, p: dict) -> tuple[bool, list, str]:
    details = []
    for sid in _text_sections(ctx):
        items = [t for t in ctx.content.get(sid, []) if str(t).strip()]
        if not items:
            title = next(s["title"] for s in ctx.sections if s["id"] == sid)
            details.append({"type": "missing_section", "section": sid, "msg": f"缺少章节内容：{title}"})
    if ctx.kpis["anomalies"] and not ctx.content.get("anomalies"):
        details.append({"type": "missing_section", "section": "anomalies", "msg": "存在异常波动但“异常与原因”为空"})
    if len(ctx.images) < p.get("min_charts", 2):
        details.append({"type": "charts", "msg": f"图表 {len(ctx.images)} 张，少于要求的 {p.get('min_charts', 2)} 张"})
    for t in p.get("required_tables", []):
        if t not in ctx.tables:
            details.append({"type": "table", "msg": f"缺少表格：{t}"})
    return not details, details, "补全缺失章节，每章至少一段"


# ---------------------------------------------------------------- 数据一致性

UP_WORDS = ["增长", "上涨", "上升", "提升", "回升", "走高", "增加", "攀升"]
DOWN_WORDS = ["下降", "下滑", "下跌", "减少", "回落", "走低", "萎缩", "降低"]
NUM_RE = re.compile(r"[+\-−]?\d+(?:[.,]\d+)*\s*(?:%|％|万元|万|元|件|单|笔)")
SIGNED_AFTER_WORD = re.compile(r"(增长|下降|下滑|下跌|上涨|上升|提升|回升|回落|减少|增加|降低|升至|降至|提高|拉低|拉高|拖累)"
                               r"([^，,；;。！？{}\n]{0,12}?)\s*\{\{\s*([^{}\s|]+)\s*\}\}")  # 允许中间隔几个字，如“拉低华东区实收 {{…}}”


@rule("data_consistency", "数据一致性")
def data_consistency(ctx: QCContext, p: dict) -> tuple[bool, list, str]:
    details: list[Any] = []
    for k in sorted(set(ctx.unresolved)):
        details.append({"type": "unresolved", "key": k, "msg": f"占位符 {{{{{k}}}}} 不存在"})
    up, down = p.get("up_words", UP_WORDS), p.get("down_words", DOWN_WORDS)
    for sid, i, fld, text in _iter_raw(ctx):
        bare = PH_RE.sub("", text)
        for m in NUM_RE.finditer(bare):
            if fld == "cause" and _quoted_from_minutes(ctx, i, m.group(0)):
                continue
            if _clause_in_minutes(ctx, bare, m.start(), m.end()):
                continue
            details.append({"type": "hardcoded", "section": sid, "index": i, "field": fld, "text": m.group(0).strip(),
                            "msg": f"正文直接写了数字“{m.group(0).strip()}”，应使用占位符引用程序计算结果"})
        for m in SIGNED_AFTER_WORD.finditer(text):
            key = m.group(3)
            leaf = key.rsplit(".", 1)[-1]
            if (leaf.endswith(("_wow", "_chg")) or leaf == "impact") and key in ctx.flat:
                details.append({"type": "sign_after_word", "section": sid, "index": i, "field": fld,
                                "text": m.group(0), "key": key,
                                "msg": f"“{m.group(1)}”后的 {{{{{key}}}}} 会带正负号，应写 {{{{{key}|abs}}}}"})
        for clause in re.split(r"[，；;。！？\n]|,(?!\d{3})", text):  # 千分位逗号不断句
            keys = [k for k in find_keys(clause) if k.endswith("_wow")]
            vals = [ctx.flat.get(k) for k in keys]
            vals = [v for v in vals if v is not None and abs(v) >= 0.005]
            if not vals:
                continue
            has_up, has_down = any(w in clause for w in up), any(w in clause for w in down)
            if has_up == has_down:
                continue
            if has_up and all(v < 0 for v in vals):
                expected = "down"
            elif has_down and all(v > 0 for v in vals):
                expected = "up"
            else:
                continue
            shown = PH_RE.sub(lambda m: fmt_value(m.group(1), ctx.flat.get(m.group(1))), clause)
            details.append({"type": "direction", "section": sid, "index": i, "field": fld, "clause": clause.strip(),
                            "expected": expected,
                            "msg": f"方向描述与数据不符：“{shown.strip()}”，实际应为{'上升' if expected == 'up' else '下降'}"})
    f = ctx.flat
    region_sum = sum(v for k, v in f.items() if k.startswith("region.") and k.endswith(".sales") and k.count(".") == 2)
    if abs(region_sum - f["total.sales"]) > 1:
        details.append({"type": "sum", "msg": f"各区域销售额之和 {region_sum:.2f} ≠ 合计 {f['total.sales']:.2f}"})
    return not details, details, "数字一律用占位符引用；涨跌描述与环比正负保持一致"


# ---------------------------------------------------------------- 异常归因有据

@rule("cause_evidence", "异常归因有据")
def cause_evidence(ctx: QCContext, p: dict) -> tuple[bool, list, str]:
    details = []
    valid = {m["no"] for m in ctx.minutes}
    items = {it.get("id"): it for it in ctx.content.get("anomalies", [])}
    pending_word = p.get("pending_word", "待确认")
    for a in ctx.kpis["anomalies"]:
        it = items.get(a["id"])
        if not it:
            details.append({"type": "cause_missing", "id": a["id"], "msg": f"异常 {a['id']} {a['label']} 未分析"})
            continue
        ev = it.get("evidence") or []
        bad = [e for e in ev if e not in valid]
        if bad:
            details.append({"type": "bad_evidence", "id": a["id"], "msg": f"{a['id']} 引用了不存在的纪要条目 {bad}"})
        if not ev and pending_word not in it.get("cause", ""):
            details.append({"type": "cause_missing", "id": a["id"],
                            "msg": f"{a['id']} 原因没有引用周会纪要，也未标注“{pending_word}”"})
    known = {a["id"] for a in ctx.kpis["anomalies"]}
    for i in items:
        if i not in known:
            details.append({"type": "unknown_anomaly", "id": i, "msg": f"{i} 不在程序识别的异常列表中"})
    return not details, details, f"每条异常的原因须引用纪要条目编号，纪要未提及的写“{pending_word}”"


# ---------------------------------------------------------------- 纪要说法与数据核对

@rule("minutes_vs_data", "纪要说法与数据核对")
def minutes_vs_data(ctx: QCContext, p: dict) -> tuple[bool, list, str]:
    """纪要中的涨跌幅说法与交易数据不一致（或实际幅度明显更大）时，报告正文必须有“数据核对”说明。
    既核对异常条目引用的纪要，也核对纪要中关于其他区域 / 门店 / 品类的说法（如非异常区域的“暴跌50%”）。"""
    from ..reconcile import extra_notes, notes

    pool = " ".join(ctx.texts.get("anomalies", []))
    found = notes(ctx.content, ctx.kpis, ctx.minutes) + extra_notes(ctx.content, ctx.kpis, ctx.minutes)
    details = [{"type": "claim_undisclosed", "id": n["id"], "msg": n["text"].replace("数据核对：", "未在报告中说明：")}
               for n in found if n["status"] != "consistent" and n["text"] not in pool]
    return not details, details, "在异常条目下补充数据核对说明"


# ---------------------------------------------------------------- 字数

@rule("length", "字数")
def length(ctx: QCContext, p: dict) -> tuple[bool, list, str]:
    details = []
    summary = ctx.texts.get("summary", [])
    n_sum = sum(len(t) for t in summary)
    if n_sum > p.get("summary_max_chars", 300):
        details.append({"type": "too_long", "section": "summary", "max_items": p.get("summary_max_items", 5),
                        "msg": f"摘要 {n_sum} 字，超过 {p.get('summary_max_chars', 300)} 字"})
    if len(summary) > p.get("summary_max_items", 5):
        details.append({"type": "too_long", "section": "summary", "max_items": p.get("summary_max_items", 5),
                        "msg": f"摘要 {len(summary)} 条，超过 {p.get('summary_max_items', 5)} 条"})
    for sid, n in (p.get("max_items") or {}).items():  # 如“下周关注事项不超过 5 条”
        if sid != "summary" and len(ctx.content.get(sid, [])) > n:
            details.append({"type": "too_long", "section": sid, "max_items": n,
                            "msg": f"{sid} {len(ctx.content[sid])} 条，超过 {n} 条"})
    total = sum(len(t) for ts in ctx.texts.values() for t in ts)
    lo, hi = p.get("total_min_chars", 500), p.get("total_max_chars", 4000)
    if not lo <= total <= hi:
        details.append({"type": "total_length", "msg": f"正文 {total} 字，不在 {lo}–{hi} 字范围内"})
    return not details, details, "压缩摘要条数与字数"


# ---------------------------------------------------------------- 篇幅（页数）

@rule("page_limit", "篇幅")
def page_limit(ctx: QCContext, p: dict) -> tuple[bool, list, str]:
    n = p.get("max_pages", 3)
    if ctx.pages is None or ctx.pages <= n:
        return True, [], ""
    chars = sum(len(PH_RE.sub("0000", t)) for _, _, _, t in _iter_raw(ctx))
    target = int(chars * n / ctx.pages * 0.85) // 50 * 50  # 表格、图表篇幅固定，只能压缩文字
    return False, [{"type": "pages", "chars": chars, "target_chars": target,
                    "msg": f"正文约 {ctx.pages} 页，超过 {n} 页；模型撰写的文字约 {chars} 字，请压缩到 {target} 字以内"}],         "表格里已有的数字不要在文字中逐项复述，每条只保留结论和原因；异常条目中的带动门店名字全部保留（可只写店名和环比），主要压缩原因和建议"


# ---------------------------------------------------------------- 超阈值对象逐一列出

@rule("threshold_coverage", "波动对象逐一列出")
def threshold_coverage(ctx: QCContext, p: dict) -> tuple[bool, list, str]:
    """环比超过阈值的区域 / 门店，必须在“异常与原因”的文字中逐一出现（作为异常条目或带动门店）。"""
    pool = " ".join(ctx.texts.get("anomalies", []))
    th = ctx.kpis["threshold"]
    details = []
    for dim in p.get("dimensions", ["region", "store"]):
        for r in ctx.kpis["tables"].get(dim, []):
            if r.get("sales_wow") is None or abs(r["sales_wow"]) <= th:
                continue
            if r["name"] not in pool:
                details.append({"type": "not_listed", "entity": r["name"],
                                "msg": f"{r['name']} 环比 {fmt_value('x_wow', r['sales_wow'])}，超过 ±{th * 100:.0f}%，"
                                       "未在“异常与原因”中说明"})
    return not details, details, "逐一列出环比超过阈值的区域和门店并说明原因"


# ---------------------------------------------------------------- 敏感信息

@rule("sensitive_words", "敏感信息")
def sensitive_words(ctx: QCContext, p: dict) -> tuple[bool, list, str]:
    details = []
    words = p.get("words", [])
    for sid, ts in ctx.texts.items():
        for t in ts:
            for m in PHONE_RE.finditer(t):
                details.append({"type": "pii", "section": sid, "text": m.group(0), "msg": f"出现手机号 {m.group(0)}"})
            for m in IDCARD_RE.finditer(t):
                details.append({"type": "pii", "section": sid, "text": m.group(0), "msg": "出现身份证号"})
            for w in words:
                if w in t:
                    details.append({"type": "word", "section": sid, "text": w, "msg": f"出现敏感词“{w}”"})
    return not details, details, "删除或脱敏敏感信息"


# ---------------------------------------------------------------- 通用：必须包含关键词（纯配置即可新增）

@rule("keyword_required", "必含关键词")
def keyword_required(ctx: QCContext, p: dict) -> tuple[bool, list, str]:
    sec = p.get("section")
    pool = " ".join(ctx.texts.get(sec, [])) if sec else " ".join(t for ts in ctx.texts.values() for t in ts)
    details = [{"type": "keyword_missing", "section": sec, "keyword": k, "msg": f"未提及“{k}”"}
               for k in p.get("keywords", []) if k not in pool]
    return not details, details, f"在{sec or '正文'}中补充：{'、'.join(p.get('keywords', []))}"


@rule("regex_forbidden", "禁止写法")
def regex_forbidden(ctx: QCContext, p: dict) -> tuple[bool, list, str]:
    details = []
    for pat in p.get("patterns", []):
        for sid, ts in ctx.texts.items():
            for t in ts:
                for m in re.finditer(pat, t):
                    details.append({"type": "forbidden", "section": sid, "text": m.group(0),
                                    "msg": f"出现不允许的写法“{m.group(0)}”"})
    return not details, details, "改写不允许的表述"


# ---------------------------------------------------------------- 审核意见已采纳（退回后自动启用）

@rule("feedback_addressed", "审核意见已采纳")
def feedback_addressed(ctx: QCContext, p: dict) -> tuple[bool, list, str]:
    details = []
    prev = ctx.prev_content or {}
    for td in ctx.todos:
        secs = [td["section"]] if td["section"] != "*" else list(ctx.content)
        changed = any(json.dumps(ctx.content.get(s), ensure_ascii=False, sort_keys=True)
                      != json.dumps(prev.get(s), ensure_ascii=False, sort_keys=True) for s in secs)
        pool = " ".join(t for s in secs for t in ctx.texts.get(s, []))
        kws = td.get("keywords", [])
        ok = changed and (not kws or any(k in pool for k in kws))
        if td.get("max_items") and "summary" in secs:
            ok = ok and len(ctx.content.get("summary", [])) <= int(td["max_items"])
        if not ok:
            details.append({"type": "feedback_not_addressed", "section": td["section"],
                            "instruction": td["instruction"],
                            "msg": f"审核意见未落实：“{td['instruction']}”（章节 {td['section']}）"})
    return not details, details, "逐条落实审核意见"
