"""复核员：用另一个模型（可单独配置）逐条核对“异常原因”与所引用的周会纪要是否相符。

撰写模型给出原因并引用纪要条目编号；程序规则只能核对编号存在、没有依据的写了“待核实”，
核对不了“引用的这条纪要是否真的支持这个原因”。这一步交给复核员模型：只读、不开放工具，
输出每条原因“有依据 / 依据不足”及理由，由质检规则 cause_review 汇总（严重级别在配置中设定）。

- 复核结果按“原因 + 引用纪要原文”的哈希缓存在任务目录：自动修正没改动的条目、断点恢复都不重复调用模型。
- 复核员不可用（未配置、接口异常、超出轮次）时不阻塞任务，只在检验记录里说明未复核。
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from ..fsutil import read_json, write_json

SYSTEM = """## 原因复核
你是经营周报的复核员。对每条异常，判断“原因”是否被所引用的周会纪要原文支持。
- supported=true：纪要原文明确提到了该对象（或其所在区域、门店）及与原因一致的事件。
- supported=false：引用的纪要与该对象无关、说的是别的事件，或与原因的说法相矛盾。
- 只核对原因与纪要是否相符，不评价写法、不核对数字（数字由程序核对）。
- reason 用一句话说明判断依据，引用纪要原文的关键词。"""

SCHEMA = {"type": "object", "required": ["verdicts"], "properties": {"verdicts": {"type": "array", "items": {
    "type": "object", "required": ["id", "supported", "reason"],
    "properties": {"id": {"type": "string"}, "supported": {"type": "boolean"}, "reason": {"type": "string"}}}}}}


def review_items(content: dict[str, Any], minutes: list[dict[str, Any]], kpis: dict[str, Any]) -> list[dict[str, Any]]:
    """需要复核的条目：引用了纪要的原因（没有引用、标注待核实的不需要复核）。"""
    by_no = {m["no"]: m["text"] for m in minutes}
    labels = {a["id"]: a.get("label", "") for a in kpis.get("anomalies", [])}
    out = []
    for it in content.get("anomalies", []):
        ev = [n for n in it.get("evidence") or [] if n in by_no]
        if not ev:
            continue
        out.append({"id": it.get("id"), "label": labels.get(it.get("id"), ""), "cause": it.get("cause", ""),
                    "evidence": [{"no": n, "text": by_no[n]} for n in ev]})
    return out


def _key(item: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(item, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:16]


def review_causes(items: list[dict[str, Any]], cache_dir: Path, run: Any) -> dict[str, dict[str, Any]]:
    """返回 {异常编号: {supported, reason, model}}。run(待复核条目) 调用复核员模型，只对缓存中没有的条目调用。"""
    cache_dir.mkdir(parents=True, exist_ok=True)
    out: dict[str, dict[str, Any]] = {}
    todo = []
    for it in items:
        p = cache_dir / f"{_key(it)}.json"
        if p.exists():
            out[it["id"]] = read_json(p)
        else:
            todo.append(it)
    if todo:
        verdicts, model = run(todo)
        by_id = {v["id"]: v for v in verdicts}
        for it in todo:
            v = by_id.get(it["id"])
            if v is None:  # 复核员漏掉的条目：不缓存，下一轮再复核
                continue
            rec = {"supported": bool(v["supported"]), "reason": v.get("reason", ""), "model": model}
            write_json(cache_dir / f"{_key(it)}.json", rec)
            out[it["id"]] = rec
    return out
