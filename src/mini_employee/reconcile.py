"""纪要口头说法与交易数据核对。

周会上区域负责人报的是口头估计（“环比下降超30%”），销售明细是 POS 交易数据。
报告以交易数据为准，并对纪要中的销售涨跌幅说法逐条核对，生成“数据核对说明”写进报告正文，
避免读者看到纪要和周报数字不一致时误以为算错。核对说明由程序生成，数字精确。

- notes()：异常条目引用的纪要，与该异常的环比核对（说明写在该异常条目下）；
- extra_notes()：整份纪要中其余的销售涨跌说法（如“华北区本周销售额暴跌50%”，而华北并不是异常），
  按句中提到的区域 / 门店 / 品类找到对应环比核对，不一致时说明写在“异常与原因”章节末尾。
"""
from __future__ import annotations

import re
from typing import Any

DOWN = ("下降", "下滑", "降幅", "暴跌", "大跌", "下跌", "骤降", "锐减", "微跌", "跌幅")
UP = ("环比", "增速", "增幅", "增长", "上涨", "上升", "暴涨", "大涨", "激增", "猛增", "微涨", "涨幅")
CLAIM_RE = re.compile(rf"({'|'.join(DOWN + UP)})[^，。；、（）()]{{0,8}}?"
                      r"(超过|超|逾|近|约|接近)?\s*([+\-]?\d+(?:\.\d+)?)\s*%(以上|左右)?")
RANGE_RE = re.compile(r"±\s*(\d+(?:\.\d+)?)\s*%\s*(?:以内|之内|内)")  # “波动均在±2%以内”
# 这些说法不是“销售额环比”，口径不同，不做比较
NOT_SALES = re.compile(r"客流|客单|毛利|周转|同比|损耗|转化|会员|折")
SALES_WORDS = re.compile(r"销售|业绩|营收|营业额")
NEGATION = re.compile(r"并非|不是|并未|没有|否认")


def _sentences(text: str) -> list[str]:
    return [s for s in re.split(r"[。；;！？\n]", text) if s.strip()]


def claims(text: str) -> list[dict[str, Any]]:
    """提取销售涨跌幅说法。句子提到“销售 / 业绩”或分句含“环比 / 增速”等才算；客流、毛利等其他口径和否定句不算。"""
    out = []
    for sentence in _sentences(text):
        sales_sentence = bool(SALES_WORDS.search(sentence))
        for clause in re.split(r"[，,（）()“”]", sentence):
            if NOT_SALES.search(clause) or NEGATION.search(clause):
                continue
            if not (re.search(r"环比|增速|增幅|降幅", clause) or sales_sentence):
                continue
            for m in CLAIM_RE.finditer(clause):
                word, qual, num, tail = m.groups()
                v = abs(float(num))
                neg = word in DOWN or num.startswith("-") or bool(re.search(r"下降|下滑|减少|跌", clause))
                kind = "atleast" if qual in ("超过", "超", "逾") or tail == "以上" else \
                    "approx" if qual in ("近", "约", "接近") or tail == "左右" else "exact"
                out.append({"text": m.group(0), "value": -v if neg else v, "kind": kind, "sentence": sentence,
                            "clause": clause.strip()})
            if sales_sentence:
                for m in RANGE_RE.finditer(clause):
                    out.append({"text": m.group(0), "value": float(m.group(1)), "kind": "within",
                                "sentence": sentence, "clause": clause.strip()})
    return out


def check(claim: dict[str, Any], actual_pct: float, tol: float = 1.0, approx: float = 5.0) -> str:
    """返回 consistent / larger（方向一致、实际幅度明显更大）/ mismatch。"""
    v = claim["value"]
    if claim["kind"] == "within":
        return "consistent" if abs(actual_pct) <= v + 0.005 else "mismatch"
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


def _who(m: dict[str, Any]) -> str:
    return re.sub(r"（.*?）|\(.*?\)", "", m["header"]) if m.get("header") else "会上"


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
                if c["kind"] == "within":
                    continue
                status = check(c, actual)
                if status == "mismatch":
                    tail = "与会上口径不一致，请以交易数据为准，并核实口径差异（如统计范围、时间区间）。"
                elif status == "larger":
                    tail = "方向与会上口径一致，但实际幅度明显更大，后续跟进请以交易数据为准。"
                else:
                    tail = "与会上口径一致。"
                out.append({
                    "id": a["id"], "minutes_no": e, "minutes_ref": m.get("ref"), "claim": c["text"], "actual": actual,
                    "status": status,
                    "text": f"数据核对：周会纪要（{m.get('ref', e)}，{_who(m)}）口头通报“{c['text']}”；按销售明细交易数据核算，"
                            f"{a['label']}销售额环比为 {actual:+.2f}%，{tail}",
                })
    return out


# ---------------------------------------------------------------- 整份纪要的其余说法

_TOTAL = re.compile(r"整体|全公司|全盘|总体|合计|大盘")
_ALL = {"各区域": "region", "各大区": "region", "各战区": "region", "各门店": "store", "各品类": "category"}


def _aliases(name: str) -> set[str]:
    """名称在口语里的常见写法：华东 → 华东区 / 华东大区 都能匹配；生鲜食品 → 生鲜；上海南京东路旗舰店 → 南京东路。"""
    out = {name}
    if name.endswith("店") and len(name) >= 6:
        out.add(name[2:6])
    elif len(name) >= 4:
        out |= {name[:2], name[-2:]}
    return out


def _mentions(text: str, names: list[str]) -> list[str]:
    return [n for n in names if any(a in text for a in _aliases(n))]


def _region_label(name: str) -> str:
    return name if name.endswith("区") else name + "区"


def _targets(claim: dict[str, Any], item: dict[str, Any], kpis: dict[str, Any]) -> list[tuple[str, float]]:
    """说法对应的对象及其销售额环比（%）：[(对象名称, 环比)]。识别不出对象时返回空，不做核对，避免张冠李戴。"""
    t, f = kpis["tables"], kpis["flat"]
    names = {d: [r["name"] for r in t.get(d, [])] for d in ("region", "store", "category")}
    s, head = claim["sentence"], f"{item.get('header') or ''} {item.get('tag') or ''}"

    def wow(key: str) -> float | None:
        v = f.get(key)
        return None if v is None else v * 100

    if claim["kind"] == "within":  # “各区域波动均在±2%以内”：逐个检查该维度
        dim = next((d for w, d in _ALL.items() if w in s), None)
        if dim:
            rows = [(_region_label(n) if dim == "region" else n, wow(f"{dim}.{n}.sales_wow")) for n in names[dim]]
        else:
            rows = [("整体", wow("total.sales_wow"))]
        return [(n, v) for n, v in rows if v is not None]
    stores = _mentions(s, names["store"])
    regions = _mentions(s, names["region"]) or _mentions(head, names["region"])
    cats = _mentions(s, names["category"])
    if len(stores) == 1:
        key, label = f"store.{stores[0]}.sales_wow", stores[0]
    elif len(regions) == 1 and len(cats) == 1:
        key, label = f"rc.{regions[0]}.{cats[0]}.sales_wow", _region_label(regions[0]) + cats[0]
    elif len(regions) == 1 and not cats:
        key, label = f"region.{regions[0]}.sales_wow", _region_label(regions[0])
    elif len(cats) == 1 and not regions:
        key, label = f"category.{cats[0]}.sales_wow", cats[0]
    elif not (stores or regions or cats) and _TOTAL.search(s):
        key, label = "total.sales_wow", "整体"
    else:
        return []
    v = wow(key)
    return [] if v is None else [(label, v)]


def extra_notes(content: dict[str, Any], kpis: dict[str, Any], minutes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """异常条目之外、纪要中与交易数据不一致的销售涨跌说法（一致的不写，避免报告冗长）。"""
    covered = {(n["minutes_no"], n["claim"]) for n in notes(content, kpis, minutes)}
    out, seen = [], set()
    for m in minutes:
        for c in claims(m["text"]):
            if (m["no"], c["text"]) in covered:
                continue
            results = [(label, v, check(c, v)) for label, v in _targets(c, m, kpis)]
            bad = [(label, v, st) for label, v, st in results if st != "consistent"]
            key = (c["text"], tuple(label for label, _, _ in bad))
            if not bad or key in seen:
                continue
            seen.add(key)
            facts = "、".join(f"{label}销售额环比为 {v:+.2f}%" for label, v, _ in bad)
            if c["kind"] == "within":
                tail = "超出该范围，请以交易数据为准。"
            elif bad[0][2] == "larger":
                tail = "方向与会上口径一致，但实际幅度明显更大，后续跟进请以交易数据为准。"
            else:
                tail = "与会上口径不一致，请以交易数据为准，并核实口径差异（如统计范围、时间区间）。"
            out.append({"id": None, "minutes_no": m["no"], "minutes_ref": m.get("ref"), "claim": c["text"],
                        "status": bad[0][2],
                        "text": f"数据核对：周会纪要（{m.get('ref', m['no'])}，{_who(m)}）口头通报“{c['clause']}”；"
                                f"按销售明细交易数据核算，{facts}，{tail}"})
    return out
