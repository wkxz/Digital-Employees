"""运行 5 条官方公开样例（A-01～A-05），把每条的交付文件与质检记录导出到 deliverables/，作为提交材料中的“公开样例用例的运行结果”。

正常任务审核通过后交付（A-01 先按赛题示例退回一次“华东区下滑原因没说清楚”，重做后再通过）；缺材料（A-03）先暂停、再补充销售明细继续；超出数据范围（A-05）停在待补材料并记录提示。
用 .env 中配置的模型（未配置时为离线 Mock）。运行：uv run python scripts/export_samples.py   或   make samples
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path

os.environ.setdefault("MINI_TODAY", "2026-11-03")  # 数据最后一周为第 44 周，落款日期放在其后

from mini_employee.config import ROOT, load_employee  # noqa: E402
from mini_employee.fsutil import read_json  # noqa: E402
from mini_employee.orchestrator import Orchestrator  # noqa: E402
from mini_employee.payloads import FeedbackCheck  # noqa: E402
from mini_employee.states import INITIATOR, LABELS, REVIEWER, S  # noqa: E402

DATA = ROOT / "data" / "official_a"
OUT = ROOT / "deliverables"
XLSX = DATA / "销售明细.xlsx"
RETURN_A01 = "华东区下滑原因没说清楚，请结合周会纪要补充"  # 赛题“场景 1：正常执行与审核退回”中的示例意见
SAMPLES = [
    ("A-01", "正常任务（含纪要与数据不一致）", "2026-W44", {"sales_file": [XLSX], "minutes_file": DATA / "周会纪要_2026W44.docx"}, None),
    ("A-02", "正常任务（含重复记录）", "2026-W43", {"sales_file": [XLSX], "minutes_file": DATA / "周会纪要_2026W43.docx"}, None),
    ("A-03", "缺材料 → 补充后继续", "2026-W44", {"minutes_file": DATA / "周会纪要_2026W44.docx"}, {"sales_file": [XLSX]}),
    ("A-04", "数据缺失与退货", "2026-W42", {"sales_file": [XLSX], "minutes_file": DATA / "周会纪要_2026W42.docx"}, None),
    ("A-05", "超出数据范围", "2026-W46", {"sales_file": [XLSX]}, None),
]


def main() -> None:
    o = Orchestrator()
    model = os.environ.get("MINI_MODEL") or (load_employee("weekly_report").raw.get("model") or {}).get("provider", "mock")
    OUT.mkdir(exist_ok=True)
    rows, totals = [], []
    for sid, name, period, files, supplement in SAMPLES:
        dst = OUT / sid
        shutil.rmtree(dst, ignore_errors=True)
        dst.mkdir(parents=True)
        tid = o.create("weekly_report", {"period": period}, files, user="张三")
        st = o.drive(tid)
        log = [f"# {sid} {name}", "", f"- 任务：{tid}", f"- 统计周期：{period}",
               f"- 上传：{'、'.join(Path(p).name for v in files.values() for p in (v if isinstance(v, list) else [v]))}"]
        if st == S.WAITING_INPUT:
            log.append(f"- 暂停（{LABELS[st]}）：{o.store.get(tid)['pause_reason']}")
            if supplement:
                log.append(f"- 补充：{'、'.join(Path(p).name for v in supplement.values() for p in v)}，从暂停处继续")
                st = o.supplement(tid, supplement, None, actor=INITIATOR)
        if st == S.PENDING_REVIEW and sid == "A-01":
            log.append(f"- 审核退回：“{RETURN_A01}”")
            try:
                st = o.review(tid, "changes", RETURN_A01, actor=REVIEWER, user="李四")
            except FeedbackCheck as e:  # 退回前检查提示意见可能有误：记录提示，按审核人确认后仍退回处理
                log.append(f"  - 退回前检查提示：{'；'.join(i['msg'] for i in e.issues)}；审核人确认后仍退回")
                st = o.review(tid, "changes", RETURN_A01, actor=REVIEWER, user="李四", confirm=True)
            log.append(f"  - 员工按意见重做 → {LABELS[st]}（V{o.store.get(tid)['version']}）")
        if st == S.PENDING_REVIEW:
            task = o.store.get(tid)
            st = o.review(tid, "approve", reviewed_sha256=task["artifact"]["sha256"], actor=REVIEWER, user="李四")
            for p in sorted((o.task_dir(tid) / "deliverable").iterdir()):
                shutil.copy(p, dst / p.name)
        final = o.store.get(tid)
        log.append(f"- 结果：{LABELS[final['state']]}" + (f"（{final['pause_reason']}）" if final["pause_reason"] else ""))
        qc = o.task_dir(tid) / "qc" / "qc_report.json"
        if qc.exists():
            rounds = read_json(qc)["rounds"]
            log.append(f"- 自动质检：{len(rounds)} 轮，" + "；".join(
                f"第 {r['round_no']} 轮{'通过' if r['passed'] else '未通过：' + '、'.join(x['name'] for x in r['results'] if not x['passed'])}"
                for r in rounds))
        calls = o.store.events(tid, ["model_called"])
        tok = sum(sum((e["payload"].get("usage") or {}).get(k, 0) for k in ("prompt_tokens", "completion_tokens"))
                  for e in calls)
        log.append(f"- 模型调用：{len(calls)} 次，共约 {tok:,} token")
        if calls:
            model = calls[0]["payload"].get("model", model)
        log += ["", "事件：", ""] + [f"- {e['ts'][11:19]} {e['type']} {e['payload'].get('from', '')}"
                                    f"{' → ' + e['payload']['to'] if e['type'] == 'state_changed' else ''}"
                                    for e in o.store.events(tid, ["state_changed", "qc_run", "delivered"])]
        totals.append(tok)
        (dst / "运行记录.md").write_text("\n".join(log) + "\n", encoding="utf-8")
        rows.append(f"| {sid} | {name} | {period} | {LABELS[final['state']]} | [{sid}/]({sid}/) |")
        print(f"{sid} {name}: {LABELS[final['state']]}")
    (OUT / "README.md").write_text("\n".join([
        "# 公开样例用例运行结果", "", f"模型：{model}　合计约 {sum(totals):,} token　由 `scripts/export_samples.py` 生成，每个目录含交付的 Word 周报、质检记录和运行记录。", "",
        "| 用例 | 内容 | 统计周期 | 结果 | 文件 |", "|---|---|---|---|---|", *rows, ""]), encoding="utf-8")
    print(f"已导出到 {OUT}")


if __name__ == "__main__":
    main()
