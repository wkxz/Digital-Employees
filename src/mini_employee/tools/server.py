"""经营周报员工的 MCP 工具服务（FastMCP）。

每个工具都有 JSON Schema 入参（由类型注解生成）和结构化出参。错误统一以前缀编码：
TRANSIENT（可重试）/ MISSING_INPUT（缺材料）/ BAD_ARGS（参数错）/ INVALID_INPUT（不合规）。

task_dir 由工具网关注入，不暴露给模型；工具只能读写 task_dir 内的文件。
独立运行：python -m mini_employee.tools.server（stdio 传输）。
"""
from __future__ import annotations

import shutil
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any

import pandas as pd
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from pydantic import Field

from .. import render as R
from ..fsutil import idempotent_write, read_json, sha256_file, sha256_json, write_json
from ..periods import parse_week
from ..placeholders import fmt_value
from . import charts as C
from . import data as D
from .errors import BAD_ARGS, INVALID_INPUT, MISSING_INPUT, encode

mcp = FastMCP("weekly-report-tools")

INJECTED = {"task_dir"}  # 由网关注入的参数，不发给模型


def _inside(task_dir: str, rel: str) -> Path:
    base = Path(task_dir).resolve()
    p = (base / rel).resolve()
    if base != p and base not in p.parents:
        raise ToolError(encode(BAD_ARGS, f"路径越界：{rel}"))
    return p


def _fail(e: D.DataError) -> ToolError:
    return ToolError(encode(e.kind, e.message))


@mcp.tool
def read_sales_data(
    task_dir: str,
    file_paths: Annotated[list[str], Field(min_length=1, description="销售明细文件（任务目录内相对路径，xlsx 或 csv），可多份，后上传的覆盖先上传的同一天同门店同品类记录")],
    period: Annotated[str, Field(description="统计周期，如 2026-W44")],
    required_columns: list[str],
    column_aliases: dict[str, list[str]],
    max_bad_ratio: float = 0.05,
    strict: bool = True,
    data_config: Annotated[dict[str, Any] | None, Field(description="明细工作表名、主数据关联（lookups）、销售目标表")] = None,
    master_file: Annotated[str | None, Field(description="主数据文件（任务目录内相对路径），上传的文件没有主数据工作表时使用")] = None,
) -> dict[str, Any]:
    """读取、合并并清洗销售明细：识别列名别名、按编码关联门店 / 商品主数据、检查必需列、去重、剔除空值与非退货负数行。
    strict=True 时缺列或脏数据过多直接报缺材料。"""
    paths = [_inside(task_dir, f) for f in file_paths]
    for f, path in zip(file_paths, paths):
        if not path.exists():
            raise ToolError(encode(MISSING_INPUT, f"找不到销售明细文件：{f}"))
    try:
        master = D.read_master(_inside(task_dir, master_file) if master_file else None)
        df, info = D.load_sales_many(paths, column_aliases, required_columns, data_config, master)
    except D.DataError as e:
        raise _fail(e) from e
    targets = info.pop("targets", [])
    info["target_weeks"] = sorted({t["week"] for t in targets})
    if info["missing_columns"]:
        if info["lookups"].get("missing_sheets"):
            info["missing_hint"] = (f"未找到主数据工作表 {info['lookups']['missing_sheets']}，"
                                    "请上传包含门店信息、商品信息工作表的 xlsx，或由管理员配置主数据文件")
        if strict:
            raise ToolError(encode(MISSING_INPUT, f"销售明细缺少必需列 {info['missing_columns']}，"
                                                  f"文件实际列为 {info['columns']}"))
        return info
    df, stats = D.clean_sales(df)
    info["cleaning"] = stats
    if strict and stats["bad_ratio"] > max_bad_ratio:
        raise ToolError(encode(MISSING_INPUT, f"异常数据占比 {stats['bad_ratio']:.1%} 超过 {max_bad_ratio:.0%}，"
                                              "请核对销售明细后重新上传"))
    week = parse_week(period)
    info["has_current_week"] = week.key in info.get("weeks", [])
    info["has_prev_week"] = week.prev.key in info.get("weeks", [])
    if strict:
        # 走到这里说明准入按 pii_policy=redact 放行：隐私列不落盘、不参与计算
        df, info["redacted_columns"] = D.redact_columns(df, info, required_columns)
        out = _inside(task_dir, "work/sales_clean.csv")
        out.parent.mkdir(parents=True, exist_ok=True)
        df.to_csv(out, index=False)
        info["table_path"] = "work/sales_clean.csv"
        write_json(_inside(task_dir, "work/targets.json"), targets)
    return info


@mcp.tool
def compute_kpis(
    task_dir: str,
    period: str,
    anomaly_threshold: Annotated[float, Field(gt=0, lt=1, description="环比异常阈值，如 0.15")],
    top_n: int = 5,
    min_base_sales: float = 0,
    dimensions: list[str] = ["region", "store", "category", "rc"],  # noqa: B006
) -> dict[str, Any]:
    """计算本周与上周的 KPI（总体 / 区域 / 门店 / 品类 / 区域×品类）及环比，识别异常波动，结果写入 work/kpis.json。"""
    src = _inside(task_dir, "work/sales_clean.csv")
    if not src.exists():
        raise ToolError(encode(MISSING_INPUT, "尚未读取销售明细"))
    df = pd.read_csv(src, parse_dates=["日期"])
    try:
        kpis = D.compute_kpis(df, parse_week(period), anomaly_threshold, top_n, min_base_sales, dimensions)
    except D.DataError as e:
        raise _fail(e) from e
    write_json(_inside(task_dir, "work/kpis.json"), kpis)
    f = kpis["flat"]
    return {
        "kpi_path": "work/kpis.json",
        "total": {k: fmt_value(k, f[f"total.{k}"]) for k in ("sales", "sales_wow", "qty", "qty_wow")},
        "anomalies": [{"id": a["id"], "label": a["label"], "wow": fmt_value("x_wow", a["wow"]),
                       "impact": fmt_value("impact", a["impact"]),
                       "drivers": [d["store"] for d in a["drivers"]]} for a in kpis["anomalies"]],
        "placeholder_count": len(f),
    }


@mcp.tool
def read_minutes(task_dir: str, file_path: Annotated[str, Field(description="周会纪要（docx 或 md）")]) -> dict[str, Any]:
    """读取周会纪要并按条编号（第 1 条、第 2 条……），写入 work/minutes.json，供引用。"""
    path = _inside(task_dir, file_path)
    if not path.exists():
        raise ToolError(encode(MISSING_INPUT, f"找不到周会纪要：{file_path}"))
    try:
        paras = D.read_minutes(path)
    except D.DataError as e:
        raise _fail(e) from e
    if not paras:
        raise ToolError(encode(MISSING_INPUT, "周会纪要内容为空，请上传包含会议内容的纪要"))
    has_pii = D.minutes_pii(paras)
    masked = 0
    for p in paras:  # 准入按 pii_policy=reject 时含隐私会直接拒绝；redact 时打码后再交给模型
        for k in ("text", "header", "tag"):
            if p.get(k) and (new := D.mask_pii(p[k])) != p[k]:
                p[k], masked = new, masked + 1
    write_json(_inside(task_dir, "work/minutes.json"), paras)
    return {"count": len(paras), "paragraphs": paras, "has_pii": has_pii, "masked": masked}


@mcp.tool
def search_minutes(
    task_dir: str,
    keywords: Annotated[list[str], Field(min_length=1, description="关键词，如 [\"华东\", \"徐汇\"]")],
) -> dict[str, Any]:
    """在周会纪要中检索包含关键词的条目，返回条目编号与原文（编号用于引用）。"""
    src = _inside(task_dir, "work/minutes.json")
    if not src.exists():
        raise ToolError(encode(MISSING_INPUT, "尚未读取周会纪要"))
    hits = D.search_minutes(read_json(src), keywords)
    return {"matches": hits, "note": "" if hits else "纪要中没有相关内容，原因应写“待确认”"}


@mcp.tool
def query_kpi(
    task_dir: str,
    prefix: Annotated[str, Field(description="占位符前缀，如 total、region.华东、store.上海徐汇店、category.生鲜、rc.华南.生鲜")],
) -> dict[str, Any]:
    """查询 KPI 及其占位符键。写作时用 {{键}} 引用数字，不要直接写数字。"""
    src = _inside(task_dir, "work/kpis.json")
    if not src.exists():
        raise ToolError(encode(MISSING_INPUT, "尚未计算 KPI"))
    flat = R.anomaly_flat(read_json(src))  # 含 anomaly.A1.* 等异常占位符，与渲染时可用的键一致
    hits = {k: fmt_value(k, v) for k, v in flat.items() if k == prefix or k.startswith(prefix + ".")}
    if not hits:
        heads = sorted({".".join(k.split(".")[:2]) for k in flat})
        raise ToolError(encode(BAD_ARGS, f"没有前缀为 {prefix} 的指标，可用前缀示例：{heads[:30]}"))
    return {"values": hits}


@mcp.tool
def render_charts(
    task_dir: str,
    charts: list[str],
    idempotency_key: Annotated[str, Field(description="幂等键：同一键重复调用不重复生成")],
) -> dict[str, Any]:
    """根据 work/kpis.json 生成图表 PNG（daily_trend / region_wow / category_wow）。"""
    kpis = read_json(_inside(task_dir, "work/kpis.json"))
    out: dict[str, str] = {}
    reused = []
    for kind in charts:
        if kind not in C.CHART_TITLES:
            raise ToolError(encode(BAD_ARGS, f"未知图表类型 {kind}，可选 {list(C.CHART_TITLES)}"))
        path = _inside(task_dir, f"work/charts/{kind}.png")
        _, hit = idempotent_write(path, sha256_json([idempotency_key, kind, kpis["flat"], kpis["daily"]]),
                                  lambda tmp, k=kind: C.render(k, kpis, tmp))
        out[kind] = f"work/charts/{kind}.png"
        if hit:
            reused.append(kind)
    return {"charts": out, "reused": reused}


@mcp.tool
def generate_report(
    task_dir: str,
    content_path: str,
    version_tag: str,
    formats: list[str],
    report_config: dict[str, Any],
    meta: dict[str, Any],
    idempotency_key: str,
) -> dict[str, Any]:
    """把内容 JSON + KPI 表格 + 图表渲染为交付文件（docx / md），同一幂等键重复调用返回已有文件。"""
    content = read_json(_inside(task_dir, content_path))
    kpis = read_json(_inside(task_dir, "work/kpis.json"))
    chart_dir = _inside(task_dir, "work/charts")
    charts = {p.stem: str(p) for p in chart_dir.glob("*.png")} if chart_dir.exists() else {}
    meta = {**meta, "generated_at": meta.get("generated_at") or datetime.now().strftime("%Y-%m-%d %H:%M")}
    mpath = _inside(task_dir, "work/minutes.json")
    minutes = read_json(mpath) if mpath.exists() else []
    blocks, texts, unresolved = R.build_blocks(content, kpis, charts, report_config, meta, minutes)
    out_dir = _inside(task_dir, f"output/{version_tag}")
    out_dir.mkdir(parents=True, exist_ok=True)
    files: dict[str, dict[str, str]] = {}
    input_hash = sha256_json([idempotency_key, content, kpis["flat"], report_config, sorted(charts),
                              {k: v for k, v in meta.items() if k != "generated_at"}])
    reused = True
    for fmt in formats:
        if fmt not in R.WRITERS:
            raise ToolError(encode(INVALID_INPUT, f"不支持的交付格式 {fmt}，可选 {list(R.WRITERS)}"))
        name = report_config["filename"].format_map(R._Default(meta)) + f".{fmt}"
        path = out_dir / name
        digest, hit = idempotent_write(path, input_hash, lambda tmp, w=R.WRITERS[fmt]: w(blocks, tmp))
        reused = reused and hit
        files[fmt] = {"path": str(path.relative_to(Path(task_dir).resolve())), "sha256": digest}
        if fmt == "md":
            for c in charts.values():
                shutil.copy(c, out_dir / Path(c).name)
    write_json(out_dir / "texts.json", {"sections": texts, "unresolved": unresolved,
                                        "images": [b[1]["caption"] for b in blocks if b[0] == "image"],
                                        "tables": [b[1]["title"] for b in blocks if b[0] == "table"]})
    return {"files": files, "texts_path": f"output/{version_tag}/texts.json", "unresolved": unresolved,
            "reused": reused}


def file_digest(task_dir: str, rel: str) -> str:
    return sha256_file(_inside(task_dir, rel))


if __name__ == "__main__":
    mcp.run()
