"""质检引擎：按配置运行规则 → 生成检验记录；程序能确定性修正的先由程序修正，其余交给模型。"""
from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from ..fsutil import read_json, write_json
from ..placeholders import fmt_value
from ..tools.data import IDCARD_RE, PHONE_RE
from .rules import RULES, QCContext, Result

PROGRAM_FIXABLE = {"hardcoded", "pii", "word", "unknown_anomaly"}


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
                key = _match_number(d["text"], flat)
                if key:
                    _replace_everywhere(c, d["text"], "{{" + key + "}}")
                    actions.append(f"数字“{d['text']}”与程序计算值一致，替换为占位符 {{{{{key}}}}}")
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


def _match_number(text: str, flat: dict[str, Any]) -> str | None:
    norm = re.sub(r"\s", "", text).replace("−", "-")
    for k, v in flat.items():
        if re.sub(r"\s", "", fmt_value(k, v)) in (norm, "+" + norm):
            return k
    for k, v in flat.items():  # 跟在“增长/下降”后的绝对值写法
        if re.sub(r"\s", "", fmt_value(k, v, absolute=True)) == norm:
            return k + "|abs"
    return None


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
