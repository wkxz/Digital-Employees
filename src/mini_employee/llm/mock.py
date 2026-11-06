"""离线 Mock 模型：规则脚本模拟“会调用工具的大模型”，走与真实模型完全相同的协议
（tool_calls → 工具结果 → JSON 输出），用于无 API Key 时开发、自测和演示。

可通过任务 faults 注入典型的模型问题，验证系统的把关能力：
  direction_error   初稿把一个下滑写成“增长”（触发数据一致性质检 → 自动修正）
  hardcode_number   初稿直接写数字而不用占位符
  forbidden_tool    首轮尝试调用未开放的工具（触发越权拒绝 + 留痕）
  loop_tools        一直调用工具不收尾（触发轮次上限 → 暂停转人工）
  bad_json_once     第一次输出不是合法 JSON（触发格式纠正）
  stubborn          质检修正时原样返回（触发修正超限 → 暂停转人工）
  llm:timeout       模型接口不可用（模拟断网）
真实模型通过 MINI_MODEL=deepseek|qwen|glm 切换，无需改动其他代码。
"""
from __future__ import annotations

import json
import re
import time
from typing import Any

from .base import LLMResponse, LLMUnavailable, ToolCall

UP_WORD, DOWN_WORD = "增长", "下降"
CAUSE_HINT = re.compile(r"原因|受|因|由于|来自|驱动|闭店|停业|装修|改造|竞品|促销|活动|入住|施工|缺货|台风|天气|客流|复工|秒杀|引流")


def cause_sentences(item: dict, names: list[str]) -> str:
    """从纪要条目中摘出讲原因的句子（模拟模型的归纳），去掉“主要原因为：”之类的引导语。"""
    sents = [s.strip() for s in re.split(r"(?<=[。；;])", item["text"]) if s.strip()]
    sents = [s for s in sents if not s.startswith(("后续", "下周", "计划", "建议", "目前"))] or sents  # 计划不是原因
    picked = [s for s in sents if CAUSE_HINT.search(s)] or sents[:1]
    out = "".join(picked)
    out = re.sub(r"(主要原因为|主要原因是|增长驱动力来自|原因是)[：:]", "", out)
    return re.sub(r"^[^：:，。]{1,8}[：:]", "", out).rstrip("。；;")


def brief(cause: str) -> str:
    """摘要里用的简短原因：取一个讲原因的分句，不带纪要里的数字。"""
    for clause in re.split(r"[，。；;]", cause):
        if CAUSE_HINT.search(clause) and not re.search(r"\d+(\.\d+)?\s*%", clause):
            return clause.strip()
    return re.split(r"[，。；;]", cause)[0].strip()


def _dir_word(direction: str) -> str:
    return {"up": UP_WORD, "down": DOWN_WORD}.get(direction, "基本持平")


class MockLLM:
    name = "mock:scripted"

    def __init__(self, faults: list[dict] | None = None):
        self.faults = {f.get("mode") if f.get("tool") == "mock" else f"{f.get('tool')}:{f.get('mode')}"
                       for f in (faults or []) if f.get("tool") in ("mock", "llm")}
        self._bad_json_done = False

    # ------------------------------------------------------------ protocol
    def chat(self, messages: list[dict[str, Any]], tools: list[dict], meta: dict[str, Any]) -> LLMResponse:
        if "llm:timeout" in self.faults:
            raise LLMUnavailable("[故障注入] 模型接口连接超时（模拟断网）")
        if "slow" in self.faults:
            time.sleep(float(meta.get("slow_seconds", 2)))
        payload = json.loads(messages[1]["content"])
        hit = re.search(r"原因(待[一-龥]{2})", str(messages[0].get("content", "")))  # 岗位说明里约定的标注词
        payload.setdefault("pending_word", hit.group(1) if hit else "待确认")
        payload["compact_phenomenon"] = "现象不必重复" in str(messages[0].get("content", ""))  # 岗位说明要求现象只写带动门店
        results = self._tool_results(messages)
        turn = meta.get("turn", 1)
        tool_names = {t["function"]["name"] for t in tools}
        purpose = meta.get("purpose")

        if "loop_tools" in self.faults:  # 模拟不听指挥的模型：最后一轮收回工具后仍要求调用工具
            return self._call("query_kpi", {"prefix": "total"}, turn)
        if "forbidden_tool" in self.faults and turn == 1:
            return self._call("generate_report", {"content_path": "x", "version_tag": "hack"}, turn)

        if purpose == "analysis":
            out = self._analysis(payload, results, turn, tool_names)
        elif purpose == "draft":
            out = self._draft(payload, results, turn, tool_names)
        elif purpose == "fix":
            out = self._fix(payload)
        else:
            out = {}
        if isinstance(out, LLMResponse):
            return out
        if "bad_json_once" in self.faults and not self._bad_json_done:
            self._bad_json_done = True
            return LLMResponse("好的，以下是结果：" + json.dumps(out, ensure_ascii=False)[:-5], usage=self._usage(messages))
        return LLMResponse("```json\n" + json.dumps(out, ensure_ascii=False, indent=1) + "\n```",
                           usage=self._usage(messages))

    @staticmethod
    def _usage(messages: list[dict]) -> dict[str, int]:
        n = sum(len(str(m.get("content", ""))) for m in messages)
        return {"prompt_tokens": n // 2, "completion_tokens": 400}

    @staticmethod
    def _call(name: str, args: dict, turn: int, n: int = 0) -> LLMResponse:
        return LLMResponse(None, [ToolCall(f"call_{turn}_{n}", name, args)])

    @staticmethod
    def _tool_results(messages: list[dict]) -> list[tuple[str, dict, Any]]:
        calls: dict[str, tuple[str, dict]] = {}
        out = []
        for m in messages:
            if m["role"] == "assistant":
                for c in m.get("tool_calls") or []:
                    calls[c["id"]] = (c["function"]["name"], json.loads(c["function"]["arguments"]))
            elif m["role"] == "tool":
                name, args = calls.get(m["tool_call_id"], ("?", {}))
                out.append((name, args, json.loads(m["content"])))
        return out

    # ------------------------------------------------------------ 异常分析
    @staticmethod
    def _feedback_for(item: dict, feedback: list[dict] | None) -> list[dict]:
        if not feedback:
            return []
        names = {item["entity"], item["label"], *item.get("search_hints", [])}
        hit = []
        for f in feedback:
            if f.get("section") not in ("anomalies", "*"):
                continue
            if any(k in n or n in k for k in f.get("keywords", []) for n in names) or not f.get("keywords"):
                hit.append(f)
        return hit

    def _analysis(self, p: dict, results: list, turn: int, tool_names: set[str]) -> Any:
        items = p.get("anomalies", [])
        if turn == 1 and items and "search_minutes" in tool_names:
            calls = []
            for i, a in enumerate(items):
                kws = list(a.get("search_hints", [])[:2])
                if self._feedback_for(a, p.get("feedback")):
                    kws += [d["store"] for d in a.get("drivers", [])] + a.get("driver_hints", [])
                calls.append(ToolCall(f"call_1_{i}", "search_minutes", {"keywords": kws}))
            return LLMResponse(None, calls)
        by_id = {}
        searches = [r for r in results if r[0] == "search_minutes"]
        for a, (_, _, res) in zip(items, searches):
            by_id[a["id"]] = res.get("matches", []) if isinstance(res, dict) else []
        out = []
        pw = p.get("pending_word", "待确认")
        for a in items:
            ph = a["placeholders"]
            matches = by_id.get(a["id"], [])
            fb = self._feedback_for(a, p.get("feedback"))
            matches = [m for m in matches if not m["text"].startswith(("下周计划", "会议时间"))]
            used = matches if fb else matches[:1]
            phen = f"{a['label']}本周{p.get('sales_word', '销售额')} {{{{{ph['sales']}}}}}，环比{_dir_word(a['direction'])} " \
                   f"{{{{{ph['wow']}|abs}}}}，影响金额 {{{{{ph['impact']}}}}}。"
            if a.get("drivers"):
                lead = "主要由" + "、".join(f"{d['store']}（{{{{{d['placeholder']}}}}}）" if "（" not in d["store"]
                                           else f"{d['store']}环比 {{{{{d['placeholder']}}}}}" for d in a["drivers"]) + "带动。"
                phen = lead if p.get("compact_phenomenon") else phen + lead
            for c in a.get("related_categories", []):
                phen += (f"受此影响，{c['category']}品类整体环比{_dir_word(c['direction'])} {{{{{c['placeholder']}|abs}}}}，"
                         f"剔除{a['entity']}区后基本持平。")
            names = [a["entity"], a["label"]]
            if used and fb:  # 按审核意见重写：逐条展开并注明出处
                cause = "；".join(f"据周会纪要{m.get('ref', '')}，" + cause_sentences(m, names) for m in used) + "。"
                evidence = [m["no"] for m in used]
            elif used:
                cause = "；".join(cause_sentences(m, names) for m in used) + "。"
                evidence = [m["no"] for m in used]
            else:
                cause, evidence = f"周会纪要未提及相关情况，原因{pw}，建议区域经理核实后补充。", []
            if a["direction"] == "down":
                sug = f"建议{a['label']}负责人跟进上述原因，制定针对性措施，并在下周会议同步恢复进展。"
            else:
                sug = f"建议评估{a['label']}增长的可持续性，总结可复制的经验。"
            out.append({"id": a["id"], "phenomenon": phen, "cause": cause, "evidence": evidence, "suggestion": sug,
                        "brief": f"原因{pw}" if not evidence else brief(cause)})
        # 意见带程序核实的问题（点名对象不在异常清单、要求写数据没有的指标等）：如实说明不适用，不编造
        resp = [{"instruction": f["instruction"], "status": "not_applicable",
                 "reason": "；".join(i["msg"] for i in f["issues"])}
                for f in p.get("feedback") or [] if f.get("issues")
                and any(i.get("kind") != "no_more_minutes" for i in f["issues"])]
        return {"anomalies": out, **({"feedback_response": resp} if resp else {})}

    # ------------------------------------------------------------ 撰写周报
    def _draft(self, p: dict, results: list, turn: int, tool_names: set[str]) -> Any:
        if turn == 1 and "query_kpi" in tool_names:
            return self._call("query_kpi", {"prefix": "total"}, turn)
        ov = p["overview"]
        tot = ov["total"]
        flip = "direction_error" in self.faults and not p.get("previous")
        regions = ov["regions"]
        cats = ov["categories"]
        an = p.get("anomalies", [])
        pw = p.get("pending_word", "待确认")
        full = ov.get("has_margin")  # 有成本、渠道、目标数据：按实收 / 毛利率 / 外卖占比 / 目标达成率写
        sec_ids = {s["id"] for s in p.get("sections", [])}
        rates = {r["name"]: r for r in ov.get("region_targets", [])}

        def region_sentence(r: dict, flip_it: bool = False) -> str:
            d = r["direction"]
            if flip_it:
                d = "up" if d == "down" else "down"
            out = f"{r['name']}区{'实收' if full else '销售额'} {{{{region.{r['name']}.sales}}}}，" \
                  f"环比{_dir_word(d)} {{{{region.{r['name']}.sales_wow|abs}}}}"
            if r["name"] in rates:
                out += f"，目标达成率 {{{{region.{r['name']}.target_rate}}}}"
            return out

        if full:
            head = (f"本周实收 {{{{total.sales}}}}，环比{_dir_word(tot['sales_direction'])} {{{{total.sales_wow|abs}}}}，"
                    f"毛利率 {{{{total.gross_margin}}}}")
            if ov.get("has_delivery"):
                head += "，外卖占比 {{total.delivery_share}}"
            if ov.get("has_target"):
                head += "，目标达成率 {{total.target_rate}}"
            summary = [head + "。"]
        else:
            summary = [f"本周总销售额 {{{{total.sales}}}}，环比{_dir_word(tot['sales_direction'])} {{{{total.sales_wow|abs}}}}；"
                       f"销量 {{{{total.qty}}}} 件，环比 {{{{total.qty_wow}}}}。"]
        for a in an[:2 if full else 3]:
            cause = a.get("brief") or a["cause"].split("；")[0].rstrip("。")
            summary.append(f"{a['label']}环比{_dir_word(a['direction'])} {{{{anomaly.{a['id']}.sales_wow|abs}}}}，"
                           f"{'原因' + pw if pw in cause else '主要原因：' + cause}。")
        moving = [r for r in regions if r["direction"] != "flat"]
        if moving:
            best = max(moving, key=lambda r: float(r["sales_wow"].rstrip("%")))
            worst = min(moving, key=lambda r: float(r["sales_wow"].rstrip("%")))
            if best["direction"] == "up" and worst["direction"] == "down" and \
                    float(best["sales_wow"].rstrip("%")) - float(worst["sales_wow"].rstrip("%")) >= 30:
                summary.insert(1, f"区域表现明显分化：{best['name']}区环比增长 {{{{region.{best['name']}.sales_wow|abs}}}}，"
                                  f"{worst['name']}区环比下降 {{{{region.{worst['name']}.sales_wow|abs}}}}。")
        summary.append(f"本周共识别 {len(an)} 项异常波动，详见“异常与原因”。" if an else "本周各项指标波动均在正常范围内。")

        word = "实收" if full else "销售额"
        overall = [f"本周 {{{{total.stores}}}}门店实现{word} {{{{total.sales}}}}，上周为 {{{{total.sales_prev}}}}，"
                   f"环比{_dir_word(tot['sales_direction'])} {{{{total.sales_wow|abs}}}}。"]
        if ov.get("has_orders"):
            overall.append(f"订单数 {{{{total.orders}}}}，环比 {{{{total.orders_wow}}}}；客单价 {{{{total.atv}}}}，"
                           f"环比 {{{{total.atv_wow}}}}。")
        if full:
            t = "毛利率 {{total.gross_margin}}（上周 {{total.gross_margin_prev}}）"
            if ov.get("has_delivery"):
                t += "，外卖占比 {{total.delivery_share}}"
            if ov.get("has_target"):
                t += "；本周目标 {{total.target}}，目标达成率 {{total.target_rate}}"
            overall.append(t + "。")
        else:
            overall.append("从日趋势看，周末（周六、周日）为销售高峰，详见下图。")

        sorted_regions = sorted(regions, key=lambda r: r["sales_rank"])
        flip_name = next((r["name"] for r in sorted_regions if r["direction"] == "down"), None) if flip else None
        reg = ["；".join(region_sentence(r, r["name"] == flip_name) for r in sorted_regions) + "。"]
        b, w = ov.get("best_store"), ov.get("worst_store")
        if b and w:
            reg.append(f"门店方面，{b['name']}环比增幅最大（{{{{store.{b['name']}.sales_wow}}}}），"
                       f"{w['name']}降幅最大（{{{{store.{w['name']}.sales_wow}}}}）。")
        if p.get("focus_region"):
            fr = next((r for r in regions if r["name"] == p["focus_region"]), None)
            if fr:
                reg.append(f"重点关注区域{fr['name']}区：本周占总销售额 {{{{region.{fr['name']}.share}}}}，"
                           f"环比{_dir_word(fr['direction'])} {{{{region.{fr['name']}.sales_wow|abs}}}}。")
        cat = ["；".join(f"{c['name']}{word} {{{{category.{c['name']}.sales}}}}，占比 {{{{category.{c['name']}.share}}}}，"
                        f"环比{_dir_word(c['direction'])} {{{{category.{c['name']}.sales_wow|abs}}}}"
                        for c in sorted(cats, key=lambda c: c["sales_rank"])) + "。"]
        if ov.get("top_items"):
            cat.append(f"本周实收前 {len(ov['top_items'])} 名单品依次为：" + "、".join(i["name"] for i in ov["top_items"]) + "。")
        for m in ov.get("margin_shifts", []):
            k = m["placeholder"]
            cat.append(f"{m['region']}区{m['category']}毛利率由上周 {{{{{k}_prev}}}} 变为 {{{{{k}}}}}"
                       f"（{{{{{k}_chg}}}}），需关注促销让利对毛利的影响。")
        nxt = [f"跟进{a['label']}：{'核实原因并补充说明' if pw in a['cause'] else '落实改善措施并跟踪效果'}。"
               for a in an if a["direction"] == "down"]
        nxt += [f"复盘{a['label']}的增长经验，评估是否推广。" for a in an if a["direction"] == "up"]
        acts = [f"会议决定：{re.sub(r'^下周计划[：:]', '', it['text'])}。" for it in p.get("action_items", [])]
        nxt = (acts + nxt)[:5] if full else nxt + acts  # 会上已定的行动项优先列入
        if not nxt:
            nxt = ["保持当前经营节奏，持续关注各区域客流变化。"]
        if "hardcode_number" in self.faults and not p.get("previous"):
            overall[0] = overall[0].replace("{{total.sales_wow|abs}}", f"{abs(tot['sales_wow_value']) * 100:.1f}%")

        if "regions" not in sec_ids and p.get("sections"):  # 没有单独的区域章节：区域情况写进整体
            overall += reg
        content = {"summary": summary, "overall": overall, "regions": reg, "categories": cat, "next_week": nxt}
        for sec in p.get("sections", []):  # 配置中新增的章节：真实模型按 guide 写，Mock 写一句占位
            if sec["id"] not in content:
                content[sec["id"]] = [f"{sec['title'].split('、')[-1]}：本周暂无需要特别说明的事项。"]
        for f in p.get("form_fields", []):  # 表单字段要求体现在指定章节
            sid = f.get("section")
            if sid in content and not any(f["label"] in t for t in content[sid]):
                content[sid].append(self._field_sentence(f, ov))
        for f in p.get("feedback") or []:
            self._apply_feedback(content, f, ov)
        return content

    @staticmethod
    def _field_sentence(f: dict, ov: dict) -> str:
        val = str(f["value"])
        if any(r["name"] == val for r in ov["regions"]):
            return f"{f['label']}{val}区：销售额 {{{{region.{val}.sales}}}}，环比 {{{{region.{val}.sales_wow}}}}。"
        if any(c["name"] == val for c in ov["categories"]):
            return (f"{f['label']}{val}：销售额 {{{{category.{val}.sales}}}}，占比 {{{{category.{val}.share}}}}，"
                    f"环比 {{{{category.{val}.sales_wow}}}}。")
        return f"{f['label']}：{val}。"

    @staticmethod
    def _apply_feedback(content: dict, f: dict, ov: dict) -> None:
        sec = f.get("section")
        if sec == "anomalies":
            return  # 异常章节的意见由异常分析步骤落实
        targets = [sec] if sec in content else (list(content) if sec == "*" else [])
        if f.get("max_items") and "summary" in targets:
            content["summary"] = content["summary"][: int(f["max_items"])]
        metric_sentence = {
            "atv": "客单价 {{total.atv}}，环比 {{total.atv_wow}}",
            "qty": "销量 {{total.qty}} 件，环比 {{total.qty_wow}}",
            "orders": "订单数 {{total.orders}}，环比 {{total.orders_wow}}",
        }
        names = {"region": [r["name"] for r in ov["regions"]], "category": [c["name"] for c in ov["categories"]]}
        for kw in f.get("keywords", []):
            sentence = None
            if kw in f.get("metrics", {}):
                sentence = f"按审核意见补充{kw}分析：{metric_sentence.get(f['metrics'][kw], '')}。"
                for t in targets[:1] or ["overall"]:
                    content[t].append(sentence)
                continue
            elif kw in names["region"]:
                sentence = f"按审核意见补充{kw}区情况：销售额 {{{{region.{kw}.sales}}}}，环比 {{{{region.{kw}.sales_wow}}}}。"
            elif kw in names["category"]:
                sentence = f"按审核意见补充{kw}品类情况：销售额 {{{{category.{kw}.sales}}}}，环比 {{{{category.{kw}.sales_wow}}}}。"
            if not sentence:
                continue
            for t in targets[:1] or ["overall"]:
                if t == "anomalies":
                    continue
                hit = next((i for i, s in enumerate(content[t]) if kw in s), None)
                if hit is None:
                    content[t].append(sentence)
                elif "按审核意见" not in content[t][hit]:  # 已提到该对象：在原句上细化，而不是原样保留
                    content[t][hit] = content[t][hit].rstrip("。") + f"；按审核意见细化{kw}的具体措施、负责人和完成时间。"

    # ------------------------------------------------------------ 质检修正
    def _fix(self, p: dict) -> dict:
        c = json.loads(json.dumps(p["content"]))
        if "stubborn" in self.faults:
            return c  # 模拟模型改不好：原样返回
        for fail in p.get("failures", []):
            for d in fail.get("details", []):
                if not isinstance(d, dict):
                    continue
                if d.get("type") == "direction":
                    sec, i = d["section"], d["index"]
                    wrong, right = (DOWN_WORD, UP_WORD) if d["expected"] == "up" else (UP_WORD, DOWN_WORD)
                    if sec == "anomalies":
                        f = d.get("field", "phenomenon")
                        c[sec][i][f] = c[sec][i][f].replace(d["clause"], d["clause"].replace(wrong, right))
                    else:
                        c[sec][i] = c[sec][i].replace(d["clause"], d["clause"].replace(wrong, right))
                elif d.get("type") == "hardcoded":
                    sec, i = d["section"], d["index"]
                    if sec == "anomalies":
                        f = d.get("field", "phenomenon")
                        c[sec][i][f] = c[sec][i][f].replace(d["text"], "")
                    else:
                        c[sec][i] = c[sec][i].replace(d["text"], "")
                elif d.get("type") == "missing_section":
                    c.setdefault(d["section"], []).append("本周该部分无特别需要说明的事项。")
                elif d.get("type") == "cause_missing":
                    for it in c.get("anomalies", []):
                        if it["id"] == d["id"]:
                            word = re.search(r"“(待[一-龥]{2})”", d.get("msg", ""))
                            it["cause"], it["evidence"] = f"周会纪要未提及相关情况，原因{word.group(1) if word else '待确认'}。", []
                elif d.get("type") == "too_long" and d.get("section") in c:
                    c[d["section"]] = c[d["section"]][: d.get("max_items", 5)]
                elif d.get("type") == "pages":  # 篇幅超限：只保留每章前两句、建议只留第一个分句
                    for sec in ("overall", "categories"):
                        if sec in c:
                            c[sec] = c[sec][:2]
                    for it in c.get("anomalies", []):
                        it["suggestion"] = re.split(r"[，；]", it.get("suggestion", ""))[0].rstrip("。") + "。"
                elif d.get("type") == "keyword_missing":
                    sec = d["section"] if d.get("section") in c else "overall"
                    c[sec].append(f"关于{d['keyword']}：本周相关指标详见上表。")
                elif d.get("type") == "feedback_not_addressed":
                    sec = d["section"] if d.get("section") in c and d["section"] != "anomalies" else "overall"
                    c[sec].append(f"按审核意见补充：{d.get('instruction', '')}")
        return c
