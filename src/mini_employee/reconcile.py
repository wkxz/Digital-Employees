"""纪要口头说法与交易数据核对。

周会上区域负责人报的是口头估计（“环比下降超30%”），销售明细是 POS 交易数据。
报告以交易数据为准，并对被引用纪要中的销售涨跌幅说法逐条核对，生成“数据核对说明”写进报告正文，
避免读者看到纪要和周报数字不一致时误以为算错。核对说明由程序生成，数字精确。
"""
from __future__ import annotations

import re
from typing import Any

CLAIM_RE = re.compile(r"(环比|增速|增幅|降幅|增长|下降|下滑|上涨|上升)[^，。；、（）()]{0,8}?"
                      r"(超过|超|逾|近|约|接近)?\s*([+\-]?\d+(?:\.\d+)?)\s*%(以上|左右)?")
# 这些说法不是“销售额环比”，口径不同，不做比较
NOT_SALES = re.compile(r"客流|客单|毛利|周转|同比|损耗|转化|会员|折")


def claims(text: str) -> list[dict[str, Any]]:
    out = []
    for clause in re.split(r"[，。；（）()]", text):
        if NOT_SALES.search(clause) or not re.search(r"环比|增速|增幅|降幅", clause):
            continue
        for m in CLAIM_RE.finditer(clause):
            word, qual, num, tail = m.groups()
            v = abs(float(num))
            neg = word in ("下降", "下滑", "降幅") or num.startswith("-") or bool(re.search(r"下降|下滑|减少", clause))
            kind = "atleast" if qual in ("超过", "超", "逾") or tail == "以上" else \
                "approx" if qual in ("近", "约", "接近") or tail == "左右" else "exact"
            out.append({"text": m.group(0), "value": -v if neg else v, "kind": kind})
    return out


def check(claim: dict[str, Any], actual_pct: float, tol: float = 1.0, approx: float = 5.0) -> str:
    """返回 consistent / larger（方向一致、实际幅度明显更大）/ mismatch。"""
    v = claim["value"]
    if (v < 0) != (actual_pct < 0):
        return "mismatch"
    a, b = abs(actual_pct), abs(v)
    if claim["kind"] == "atleast":
        ok = a >= b - tol
    elif claim["kind"] == "approx":
        ok = abs(a - b) <= approx
    else:
        ok = abs(a - b) <= tol
    if not ok:
        return "mismatch"
    # “超30%”实际 -53%：说法没错但明显低估，值得提示；“超过40%”实际 48% 属正常表述范围
    return "larger" if a - b > approx and a > b * 1.25 else "consistent"


def notes(content: dict[str, Any], kpis: dict[str, Any], minutes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """对每条异常引用的纪要条目逐条核对，返回核对结果（含报告用的说明文字）。"""
    by_no = {m["no"]: m for m in minutes}
    known = {a["id"]: a for a in kpis["anomalies"]}
    out = []
    for it in content.get("anomalies", []):
        a = known.get(it.get("id"))
        if not a:
            continue
        actual = a["wow"] * 100
        for e in it.get("evidence") or []:
            m = by_no.get(e)
            if not m:
                continue
            for c in claims(m["text"]):
                status = check(c, actual)
                who = re.sub(r"（.*?）|\(.*?\)", "", m["header"]) if m.get("header") else "会上"
                if status == "mismatch":
                    tail = "与会上口径不一致，请以交易数据为准，并核实口径差异（如统计范围、时间区间）。"
                elif status == "larger":
                    tail = "方向与会上口径一致，但实际幅度明显更大，后续跟进请以交易数据为准。"
                else:
                    tail = "与会上口径一致。"
                out.append({
                    "id": a["id"], "minutes_ref": m.get("ref"), "claim": c["text"], "actual": actual, "status": status,
                    "text": f"数据核对：周会纪要（{m.get('ref', e)}，{who}）口头通报“{c['text']}”；按销售明细交易数据核算，"
                            f"{a['label']}销售额环比为 {actual:+.2f}%，{tail}",
                })
    return out
