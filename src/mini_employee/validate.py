"""员工配置校验：现场改完 employee.yaml / SKILL.md 后先跑 mini check-config，几秒内发现写错的地方。

errors：会导致任务失败或行为不符合预期，加载配置时直接拒绝；warnings：能运行，但可能不是想要的效果。
"""
from __future__ import annotations

import asyncio
import string
from functools import lru_cache
from typing import Any

FIELD_TYPES = {"iso_week", "file", "enum", "string"}
STEP_TYPES = {"tool", "agent", "render"}
PURPOSES = {"analysis", "draft"}
SECTION_KINDS = {"paragraphs", "bullets", "anomalies", "appendix", "data_quality"}
TEXT_KINDS = {"paragraphs", "bullets"}
SEVERITIES = {"blocking", "warning"}
PROVIDERS = {"mock", "openai_compatible", "deepseek", "qwen", "glm"}
TABLES = {"kpi_overall", "region_table", "store_rank", "category_table", "top_items", "anomaly_table"}
LIMITS = ("max_model_turns_per_step", "max_step_seconds", "max_task_seconds", "tool_max_retries",
          "qc_max_fix_rounds", "rework_max_rounds")
FIX_SKILL_SECTIONS = ("岗位职责", "撰写周报", "注意事项", "质检修正")  # 自动修正步骤固定使用
FIX_DEFAULT = "根据 failures 中列出的问题修改 content，只改有问题的句子，其余保持不变，输出完整的 content。"  # SKILL.md 没有“质检修正”时使用
META_KEYS = {"period", "period_compact", "period_label", "version", "generated_at", "sources"}  # 渲染时可引用的内置字段


class ConfigError(Exception):
    def __init__(self, employee_id: str, errors: list[str]):
        super().__init__(f"员工 {employee_id} 的配置有误：" + "；".join(errors))
        self.errors = errors


@lru_cache(maxsize=1)
def tool_names() -> frozenset[str]:
    from .tools.server import mcp
    return frozenset(t.name for t in asyncio.run(mcp.list_tools()))


def _template_keys(tpl: Any) -> list[str]:
    try:
        return [f.split(".")[0].split("[")[0] for _, f, _, _ in string.Formatter().parse(str(tpl)) if f]
    except ValueError:
        return ["<格式错误>"]


def validate(raw: dict[str, Any], skill_sections: dict[str, str]) -> tuple[list[str], list[str]]:
    from .qc.rules import RULES
    from .render import WRITERS
    from .tools.charts import CHART_TITLES

    errors: list[str] = []
    warnings: list[str] = []
    err, warn = errors.append, warnings.append

    if not isinstance(raw, dict):
        return ["employee.yaml 的顶层应是键值对（id: …、form: …）"], warnings
    for k in ("id", "name", "form", "admission", "tools", "steps", "quality_checks", "limits", "report"):
        if k not in raw:
            err(f"缺少顶层配置项 {k}")
    if errors:
        return errors, warnings

    report = raw["report"] or {}
    sections = report.get("sections") or []
    section_ids = [s.get("id") for s in sections if isinstance(s, dict)]
    text_sections = {s.get("id") for s in sections if isinstance(s, dict) and s.get("kind", "paragraphs") in TEXT_KINDS}

    # ---- form
    keys: list[str] = []
    for i, f in enumerate(raw["form"] or [], 1):
        where = f"form 第 {i} 项（{f.get('key', '?')}）" if isinstance(f, dict) else f"form 第 {i} 项"
        if not isinstance(f, dict):
            err(f"{where}应写成 {{key: ..., label: ..., type: ...}}")
            continue
        for k in ("key", "label", "type"):
            if not f.get(k):
                err(f"{where}缺少 {k}")
        keys.append(f.get("key"))
        t = f.get("type")
        if t and t not in FIELD_TYPES:
            err(f"{where}的 type “{t}” 不支持，可选 {sorted(FIELD_TYPES)}")
        if t == "enum" and not (isinstance(f.get("options"), list) and f["options"]):
            err(f"{where}是 enum，须提供 options 列表")
        if t == "file" and not f.get("ext"):
            err(f"{where}是 file，须提供 ext（允许的扩展名）")
        if f.get("section") and f["section"] not in text_sections:
            err(f"{where}的 section “{f['section']}” 不是由模型撰写的章节，可选 {sorted(text_sections)}")
        if f.get("section") and t not in ("enum", "string"):
            warn(f"{where}配置了 section，但只有 enum / string 字段会自动加质检")
    dup = sorted({k for k in keys if k and keys.count(k) > 1})
    if dup:
        err(f"form 中字段 key 重复：{dup}")
    for k in ("period", "sales_file", "minutes_file"):
        if k not in keys:
            err(f"form 缺少程序依赖的字段 {k}")

    # ---- tools / steps
    known_tools = tool_names()
    emp_tools = raw["tools"] or []
    for t in emp_tools:
        if t not in known_tools:
            err(f"tools 中的 {t} 不存在，可用工具：{sorted(known_tools)}")
    step_ids: list[str] = []
    purposes: set[str] = set()
    for i, s in enumerate(raw["steps"] or [], 1):
        where = f"steps 第 {i} 步（{s.get('id', '?')}）"
        step_ids.append(s.get("id"))
        st = s.get("type")
        if st not in STEP_TYPES:
            err(f"{where}的 type “{st}” 不支持，可选 {sorted(STEP_TYPES)}")
            continue
        if st in ("tool", "render"):
            tool = s.get("tool")
            if not tool:
                err(f"{where}缺少 tool")
            elif tool not in emp_tools:
                err(f"{where}使用的工具 {tool} 不在 tools 列表中")
            for ref in _refs(s.get("args", {})):
                sec, _, key = ref[1:].partition(".")
                if sec == "form":
                    if key not in keys:
                        err(f"{where}的参数 {ref} 引用了不存在的表单字段")
                elif not isinstance(raw.get(sec), dict) or (key and key not in raw[sec]):
                    err(f"{where}的参数 {ref} 引用了不存在的配置项")
        sk = s.get("skip_if_empty")
        if sk is not None and not (isinstance(sk, str) and sk.startswith("$form.") and sk[6:] in keys):
            err(f"{where}的 skip_if_empty 须引用表单字段（如 $form.minutes_file），收到：{sk}")
        if st == "render" and s.get("tool") != "generate_report":
            err(f"{where}是 render 步骤，tool 须为 generate_report")
        if st == "agent":
            if s.get("purpose") not in PURPOSES:
                err(f"{where}的 purpose “{s.get('purpose')}” 不支持，可选 {sorted(PURPOSES)}")
            purposes.add(s.get("purpose"))
            for t in s.get("tools", []):
                if t not in emp_tools:
                    err(f"{where}开放的工具 {t} 不在 tools 列表中")
            names = s.get("skill_sections") or []
            if not names:
                err(f"{where}没有配置 skill_sections，模型将拿不到岗位要求")
            for n in names:
                if n not in skill_sections:
                    err(f"{where}的 skill_sections “{n}” 在 SKILL.md 中不存在（现有：{list(skill_sections)}）")
    dup = sorted({k for k in step_ids if k and step_ids.count(k) > 1})
    if dup:
        err(f"steps 中步骤 id 重复：{dup}")
    if not {"load", "kpi", "minutes"} <= set(step_ids):
        err("steps 须包含 load / kpi / minutes 三个读取数据的步骤")
    if not ({"analysis", "draft"} <= purposes and "render" in {s.get("type") for s in raw["steps"]}):
        err("steps 须包含 analysis、draft 两个 agent 步骤和一个 render 步骤")
    for n in FIX_SKILL_SECTIONS:
        if n not in skill_sections:
            warn(f"SKILL.md 缺少“{n}”小节，自动修正步骤会用到")

    # ---- quality_checks
    for i, q in enumerate(raw["quality_checks"] or [], 1):
        rid = q.get("rule") if isinstance(q, dict) else None
        where = f"quality_checks 第 {i} 条（{rid}）"
        if rid not in RULES:
            err(f"{where}的规则不存在，可用规则：{sorted(RULES)}")
            continue
        if q.get("severity", "blocking") not in SEVERITIES:
            err(f"{where}的 severity 须为 blocking 或 warning")
        p = q.get("params") or {}
        if rid == "keyword_required":
            if not p.get("keywords"):
                err(f"{where}须在 params 中提供 keywords 列表")
            if p.get("section") and p["section"] not in section_ids:
                err(f"{where}的 section “{p['section']}” 不存在，可选 {section_ids}")
        if rid == "regex_forbidden":
            import re
            for pat in p.get("patterns") or []:
                try:
                    re.compile(pat)
                except re.error as e:
                    err(f"{where}的正则 {pat!r} 有误：{e}")
            if not p.get("patterns"):
                err(f"{where}须在 params 中提供 patterns 列表")

    # ---- limits / analysis
    lim = raw["limits"] or {}
    for k in LIMITS:
        v = lim.get(k)
        if not isinstance(v, (int, float)) or isinstance(v, bool) or v < 0:
            err(f"limits.{k} 须为非负数（当前：{v!r}）")
    th = (raw.get("analysis") or {}).get("anomaly_threshold")
    if th is not None and not (isinstance(th, (int, float)) and 0 < th < 1):
        err(f"analysis.anomaly_threshold 须在 0 和 1 之间（如 0.15 表示 15%），当前：{th!r}")
    adm = raw["admission"] or {}
    for k in ("required_columns", "column_aliases", "max_bad_row_ratio"):
        if k not in adm:
            err(f"admission 缺少 {k}")
    if adm.get("pii_policy", "reject") not in ("reject", "redact"):
        err("admission.pii_policy 须为 reject 或 redact")

    # ---- report
    for k in ("title", "filename", "output_formats", "sections"):
        if not report.get(k):
            err(f"report 缺少 {k}")
    fmts = report.get("output_formats") or []
    for f in fmts:
        if f not in WRITERS:
            err(f"report.output_formats 中的 {f} 不支持，可选 {sorted(WRITERS)}")
    if fmts and fmts[0] != "docx":
        warn(f"report.output_formats 第一个是审核用的主文件，当前为 {fmts[0]}，建议保持 docx 在首位")
    allowed = set(keys) | META_KEYS
    for k in ("title", "filename", "footer"):
        for name in _template_keys(report.get(k, "")):
            if name not in allowed:
                err(f"report.{k} 引用了 {{{name}}}，它不是表单字段或内置字段（可用：{sorted(allowed)}）")
    for i, s in enumerate(sections, 1):
        where = f"report.sections 第 {i} 项（{s.get('id', '?')}）"
        for k in ("id", "title"):
            if not s.get(k):
                err(f"{where}缺少 {k}")
        if s.get("kind", "paragraphs") not in SECTION_KINDS:
            err(f"{where}的 kind “{s.get('kind')}” 不支持，可选 {sorted(SECTION_KINDS)}")
        for t in s.get("tables", []):
            if t not in TABLES:
                err(f"{where}的表格 {t} 不存在，可选 {sorted(TABLES)}")
        for c in s.get("charts", []):
            if c not in CHART_TITLES:
                err(f"{where}的图表 {c} 不存在，可选 {sorted(CHART_TITLES)}")
    dup = sorted({k for k in section_ids if k and section_ids.count(k) > 1})
    if dup:
        err(f"report.sections 中章节 id 重复：{dup}")
    if "anomalies" not in section_ids:
        err("report.sections 须包含 id 为 anomalies 的章节（异常与原因）")
    charts_step = next((s for s in raw["steps"] if s.get("tool") == "render_charts"), None)
    rendered = set((charts_step or {}).get("args", {}).get("charts", []))
    for s in sections:
        for c in s.get("charts", []):
            if c in CHART_TITLES and c not in rendered:
                warn(f"章节 {s.get('id')} 引用了图表 {c}，但 render_charts 步骤没有生成它")

    # ---- feedback / model
    for sid in ((raw.get("feedback") or {}).get("section_aliases") or {}):
        if sid not in section_ids:
            warn(f"feedback.section_aliases 中的 {sid} 不是报告章节 id")
    prov = (raw.get("model") or {}).get("provider", "mock")
    if prov not in PROVIDERS:
        err(f"model.provider “{prov}” 不支持，可选 {sorted(PROVIDERS)}")
    return errors, warnings


def _refs(v: Any) -> list[str]:
    if isinstance(v, str):
        return [v] if v.startswith("$") else []
    if isinstance(v, dict):
        return [r for x in v.values() for r in _refs(x)]
    if isinstance(v, list):
        return [r for x in v for r in _refs(x)]
    return []
