"""质检引擎：按配置运行规则 → 生成检验记录；程序能确定性修正的先由程序修正，其余交给模型。"""
from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from ..fsutil import read_json, write_json
from ..payloads import store_core
from ..placeholders import PH_RE, fmt_value
from ..tools.data import IDCARD_RE, PHONE_RE
from .rules import RULES, QCContext, Result

PROGRAM_FIXABLE = {"hardcoded", "pii", "word", "unknown_anomaly", "sign_after_word"}


def run_rules(ctx: QCContext, specs: list[dict[str, Any]]) -> list[Result]:
    results = []
    for spec in specs:
        rid = spec["rule"]
        if rid not in RULES:
            raise KeyError(f"未知质检规则 {rid}，可用：{sorted(RULES)}")
        name, fn = RULES[rid]
        passed, details, hint = fn(ctx, spec.get("params", {}))
        results.append(Result(rid, spec.get("name", name), passed, spec.get("severity", "blocking"), details, hint))
    return results


def blocking_failures(results: list[Result]) -> list[Result]:
    return [r for r in results if not r.passed and r.severity == "blocking"]


def program_fix(content: dict[str, Any], failures: list[dict[str, Any]], flat: dict[str, Any],
                words: list[str]) -> tuple[dict[str, Any], list[str], list[dict[str, Any]]]:
    """确定性修正：硬编码数字→占位符（数值能对上时）、脱敏、删除不存在的异常条目。
    返回 (新内容, 修正动作说明, 仍需模型处理的失败项)。"""
    c = json.loads(json.dumps(content, ensure_ascii=False))
    actions: list[str] = []
    remaining: list[dict[str, Any]] = []
    for f in failures:
        left = []
        for d in f["details"]:
            t = d.get("type") if isinstance(d, dict) else None
            if t == "hardcoded":
                text = _item_text(c, d)
                clause = _clause_of(text, d["text"]) if text is not None else None
                key = _match_number(d["text"], flat, clause) if clause is not None else None
                if key:
                    _set_item_text(c, d, _replace_in_clause(text, d["text"], "{{" + key + "}}"))
                    actions.append(f"数字“{d['text']}”与该句所述对象的程序计算值一致，替换为占位符 {{{{{key}}}}}")
                    continue
                left.append(d)
            elif t == "sign_after_word":
                text = _item_text(c, d)
                if text is not None and d["text"] in text:
                    _set_item_text(c, d, text.replace(d["text"], d["text"].replace(d["key"], d["key"] + "|abs")))
                    actions.append(f"“{d['text']}”改为不带正负号的写法")
                    continue
                left.append(d)
            elif t in ("pii", "word"):
                _replace_everywhere(c, d["text"], _mask(d["text"]))
                actions.append(f"脱敏：{_mask(d['text'])}")
            elif t == "unknown_anomaly":
                c["anomalies"] = [it for it in c.get("anomalies", []) if it.get("id") != d["id"]]
                actions.append(f"删除不存在的异常条目 {d['id']}")
            else:
                left.append(d)
        if left:
            remaining.append({**f, "details": left})
    for w in words:
        _replace_everywhere(c, w, _mask(w))
    return c, actions, remaining


def _mask(s: str) -> str:
    if PHONE_RE.fullmatch(s):
        return s[:3] + "****" + s[-4:]
    if IDCARD_RE.fullmatch(s):
        return s[:4] + "**********" + s[-4:]
    return "*" * len(s)


_CLAUSE_SPLIT = re.compile(r"([，,；;。！？\n])")
_TOTAL_WORDS = ("整体", "总", "合计", "全部", "大盘", "全公司")


def _item_text(c: dict[str, Any], d: dict[str, Any]) -> str | None:
    try:
        item = c[d["section"]][d["index"]]
        return item.get(d.get("field") or "phenomenon") if isinstance(item, dict) else item
    except (KeyError, IndexError, TypeError):
        return None


def _set_item_text(c: dict[str, Any], d: dict[str, Any], text: str) -> None:
    item = c[d["section"]][d["index"]]
    if isinstance(item, dict):
        item[d.get("field") or "phenomenon"] = text
    else:
        c[d["section"]][d["index"]] = text


def _clause_of(text: str, number: str) -> str | None:
    """数字所在的分句（不含占位符本身）。"""
    for clause in _CLAUSE_SPLIT.split(text):
        if number in PH_RE.sub("", clause):
            return clause
    return None


def _replace_in_clause(text: str, number: str, new: str) -> str:
    """只替换数字所在分句中的这一处，不影响其他句子里碰巧相同的数字（如原样引用的纪要数字）。"""
    parts = _CLAUSE_SPLIT.split(text)
    for i, clause in enumerate(parts):
        if number in PH_RE.sub("", clause):
            parts[i] = clause.replace(number, new, 1)
            break
    return "".join(parts)


def _entities(key: str) -> list[str] | None:
    """占位符键对应的对象名：total → []；region/store/category → [名称]；rc → [区域, 品类]；其他不参与匹配。"""
    parts = key.split(".")
    if parts[0] == "total":
        return []
    if parts[0] in ("region", "store", "category") and len(parts) >= 3:
        return [".".join(parts[1:-1])]
    if parts[0] == "rc" and len(parts) == 4:
        return parts[1:3]
    return None


def _mentioned(name: str, clause: str) -> bool:
    return name in clause or store_core(name) in clause


def _match_number(text: str, flat: dict[str, Any], clause: str) -> str | None:
    """只在该句提到的对象（区域 / 门店 / 品类）的指标中找数值相同的键；句中没提到具体对象时只看整体指标。
    唯一匹配才替换，匹配不到或有歧义的交给模型改——避免把写错的数字换成碰巧数值相同的其他指标。"""
    norm = re.sub(r"\s", "", text).replace("−", "-")
    named = {k: e for k, e in ((k, _entities(k)) for k in flat) if e}
    cands = [k for k, e in named.items() if all(_mentioned(n, clause) for n in e)]
    if not cands or any(w in clause for w in _TOTAL_WORDS):
        cands += [k for k in flat if _entities(k) == []]
    hits = set()
    for k in cands:
        v = flat[k]
        if re.sub(r"\s", "", fmt_value(k, v)) in (norm, "+" + norm):
            hits.add(k)
        elif isinstance(v, (int, float)) and re.sub(r"\s", "", fmt_value(k, v, absolute=True)) == norm:
            hits.add(k + "|abs")
    return hits.pop() if len(hits) == 1 else None


def _replace_everywhere(obj: Any, old: str, new: str) -> Any:
    if isinstance(obj, dict):
        for k in obj:
            obj[k] = _replace_everywhere(obj[k], old, new)
    elif isinstance(obj, list):
        for i in range(len(obj)):
            obj[i] = _replace_everywhere(obj[i], old, new)
    elif isinstance(obj, str):
        return obj.replace(old, new)
    return obj


# ---------------------------------------------------------------- 检验记录

def append_record(qc_dir: Path, record: dict[str, Any]) -> dict[str, Any]:
    path = qc_dir / "qc_report.json"
    data = read_json(path) if path.exists() else {"rounds": []}
    data["rounds"].append(record)
    write_json(path, data)
    (qc_dir / "质检记录.md").write_text(record_markdown(data), encoding="utf-8")
    return data


def record_markdown(data: dict[str, Any]) -> str:
    out = ["# 质检记录", ""]
    for r in data["rounds"]:
        mark = "✅ 通过" if r["passed"] else "❌ 未通过"
        out += [f"## 第 {r['round_no']} 次质检 · 版本 V{r['version']} · {r['ts']} · {mark}", "",
                "| 规则 | 级别 | 结果 | 说明 |", "|---|---|---|---|"]
        for res in r["results"]:
            ok = "✓" if res["passed"] else "✗"
            msg = "；".join(d["msg"] if isinstance(d, dict) else str(d) for d in res["details"][:5]) or "—"
            if len(res["details"]) > 5:
                msg += f"（等 {len(res['details'])} 项）"
            out.append(f"| {res['name']}（{res['rule']}） | {res['severity']} | {ok} | {msg.replace('|', '/')} |")
        if r.get("fix_actions"):
            out += ["", "修正动作：", *[f"- {a}" for a in r["fix_actions"]]]
        out.append("")
    return "\n".join(out)


def now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
