"""现场变更预演：只改 employee.yaml，不改代码，验证新章节、新表单字段、新导出格式都能生效。

每个用例复制一份员工配置到临时目录再修改，不影响仓库里的配置。
"""
import shutil
from pathlib import Path

import pytest
import yaml

from mini_employee.states import S

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def edit_employee(tmp_path, monkeypatch):
    import mini_employee.config as C
    base = tmp_path / "employees"
    shutil.copytree(ROOT / "employees", base)
    monkeypatch.setattr(C, "EMPLOYEES_DIR", base)
    path = base / "weekly_report" / "employee.yaml"

    def edit(fn):
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        fn(raw)
        path.write_text(yaml.safe_dump(raw, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return edit


def _run(orch, sample_files, **fields):
    tid = orch.create("weekly_report", {"period": "2026-W44", **fields}, sample_files)
    return tid, orch.drive(tid)


def _text(orch, tid):
    from mini_employee.runner import _report_text
    return _report_text(orch, tid)


def _categories(kpis):
    return [r["name"] for r in kpis["tables"]["category"]]


# ---------------------------------------------------------------- 新增章节

def test_new_section_only_needs_config(edit_employee, orch, sample_files):
    edit_employee(lambda r: r["report"]["sections"].insert(-1, {
        "id": "risks", "title": "七、风险提示", "kind": "bullets", "guide": "列出下周可能影响销售的风险"}))
    tid, state = _run(orch, sample_files)
    assert state == S.PENDING_REVIEW
    assert "七、风险提示" in _text(orch, tid)
    from mini_employee.fsutil import read_json
    content = read_json(orch.task_dir(tid) / orch.store.get(tid)["artifact"]["content_path"])
    assert content["risks"], "新章节应有模型撰写的内容"
    calls = [e["payload"] for e in orch.store.events(tid, ["model_called"]) if e["payload"]["purpose"] == "draft"]
    assert calls  # 撰写步骤确实执行了；章节说明 guide 随 sections 传给模型


# ---------------------------------------------------------------- 表单加字段

def test_form_field_with_section_reaches_report_and_is_checked(edit_employee, orch, sample_files, kpis):
    cats = _categories(kpis)
    edit_employee(lambda r: r["form"].append({
        "key": "focus_category", "label": "重点关注品类", "type": "enum", "options": cats, "required": False,
        "guide": "在“品类表现”中单独写一句该品类的销售额、占比和环比", "section": "categories"}))
    tid, state = _run(orch, sample_files, focus_category=cats[0])
    assert state == S.PENDING_REVIEW
    assert f"重点关注品类{cats[0]}" in _text(orch, tid)
    from mini_employee.fsutil import read_json
    rounds = read_json(orch.task_dir(tid) / "qc" / "qc_report.json")["rounds"]
    rule = [r for r in rounds[-1]["results"] if r["name"] == "表单要求：重点关注品类"]
    assert rule and rule[0]["passed"], "配置了 section 的字段应自动加一条质检"


def test_form_field_in_footer_template(edit_employee, orch, sample_files):
    def change(r):
        r["form"].append({"key": "recipient", "label": "报告接收人", "type": "string", "required": False})
        r["report"]["footer"] = "报送：{recipient}。" + r["report"]["footer"]
    edit_employee(change)
    tid, state = _run(orch, sample_files, recipient="运营总监")
    assert state == S.PENDING_REVIEW and "报送：运营总监。" in _text(orch, tid)


def test_invalid_enum_value_is_rejected(edit_employee, orch, sample_files, kpis):
    edit_employee(lambda r: r["form"].append({"key": "focus_category", "label": "重点关注品类", "type": "enum",
                                              "options": _categories(kpis), "required": False}))
    _, state = _run(orch, sample_files, focus_category="不存在的品类")
    assert state == S.REJECTED


# ---------------------------------------------------------------- 导出格式

def test_extra_export_formats(edit_employee, orch, sample_files):
    edit_employee(lambda r: r["report"].__setitem__("output_formats", ["docx", "md", "html", "pdf"]))
    tid, state = _run(orch, sample_files)
    assert state == S.PENDING_REVIEW
    orch.review(tid, "approve", reviewed_sha256=orch.store.get(tid)["artifact"]["sha256"])
    out = orch.task_dir(tid) / "deliverable"
    names = {p.suffix for p in out.iterdir()}
    assert {".docx", ".md", ".html", ".pdf", ".png"} <= names
    assert (out / "经营周报_2026W44.pdf").read_bytes().startswith(b"%PDF")
    assert "data:image/png;base64," in (out / "经营周报_2026W44.html").read_text(encoding="utf-8")
    md = (out / "经营周报_2026W44.md").read_text(encoding="utf-8")
    import re
    for img in re.findall(r"!\[[^\]]*\]\(([^)]+)\)", md):
        assert (out / img).exists(), f"Markdown 引用的图片 {img} 不在交付包里"


def test_pdf_writer_is_deterministic(tmp_path):
    from mini_employee.render import write_pdf
    blocks = [("title", "经营周报"), ("p", "中文段落" * 40), ("table", {"title": "表", "columns": ["区域", "销售额"],
                                                                     "rows": [["华东", "1.0 万元"]] * 60})]
    write_pdf(blocks, tmp_path / "a.pdf")
    write_pdf(blocks, tmp_path / "b.pdf")
    assert (tmp_path / "a.pdf").read_bytes() == (tmp_path / "b.pdf").read_bytes()


def test_unsupported_format_is_rejected_before_running(edit_employee, orch, sample_files):
    from mini_employee.validate import ConfigError
    edit_employee(lambda r: r["report"].__setitem__("output_formats", ["docx", "pptx"]))
    with pytest.raises(ConfigError) as e:
        _run(orch, sample_files)
    assert any("pptx 不支持" in x for x in e.value.errors)


# ---------------------------------------------------------------- 配置校验：写错的地方在发起任务前就报出来

@pytest.mark.parametrize("change, expected", [
    (lambda r: r["quality_checks"].append({"rule": "keyword_requird", "params": {"keywords": ["客单价"]}}),
     "规则不存在"),
    (lambda r: r["steps"][4].__setitem__("skill_sections", ["岗位职责", "异常分折"]), "“异常分折” 在 SKILL.md 中不存在"),
    (lambda r: r["form"].append({"key": "x", "label": "X", "type": "enum", "options": ["A"], "section": "nope"}),
     "section “nope”"),
    (lambda r: r["report"].__setitem__("footer", "报送：{recipient}"), "引用了 {recipient}"),
    (lambda r: r["report"]["sections"].insert(0, {"id": "risks", "kind": "bullets"}), "缺少 title"),
    (lambda r: r["analysis"].__setitem__("anomaly_threshold", 15), "anomaly_threshold"),
    (lambda r: r["steps"][5].__setitem__("tools", ["query_kpi", "send_email"]), "send_email 不在 tools 列表中"),
])
def test_config_mistakes_are_reported(edit_employee, change, expected):
    from mini_employee.cli import check_config
    from mini_employee.config import load_employee
    from mini_employee.validate import ConfigError
    edit_employee(change)
    with pytest.raises(ConfigError) as e:
        load_employee("weekly_report")
    assert any(expected in x for x in e.value.errors), e.value.errors
    assert check_config("weekly_report") == 4


def test_current_config_is_valid(capsys):
    from mini_employee.cli import check_config
    assert check_config("weekly_report") == 0
    assert "校验通过" in capsys.readouterr().out


def test_config_broken_mid_task_does_not_affect_running_task(edit_employee, orch, sample_files):
    faults = {"faults": [{"tool": "llm", "mode": "timeout"}]}
    keep = orch.create("weekly_report", {"period": "2026-W44"}, sample_files, faults)
    fresh = orch.create("weekly_report", {"period": "2026-W44"}, sample_files, faults)
    assert orch.drive(keep) == S.SUSPENDED and orch.drive(fresh) == S.SUSPENDED  # 执行到一半暂停
    edit_employee(lambda r: r["quality_checks"].append({"rule": "no_such_rule"}))  # 期间有人把配置改错了
    assert orch.retry(keep, clear_faults=True) == S.PENDING_REVIEW  # 沿用发起时的配置快照，不受影响
    assert orch.retry(fresh, clear_faults=True, refresh_config=True) == S.SUSPENDED  # 主动改用最新配置
    reason = orch.store.get(fresh)["pause_reason"]
    assert "配置有误" in reason and "no_such_rule" in reason and "使用最新配置" in reason


# ---------------------------------------------------------------- 现场演练：企业数据列名不同，只加别名

def test_live_alias_change_makes_renamed_columns_work(edit_employee, orch, tmp_path):
    import pandas as pd
    src = ROOT / "data" / "official_a"
    book = pd.read_excel(src / "销售明细.xlsx", sheet_name=None, engine="calamine")
    book["销售明细"] = book["销售明细"].rename(columns={"门店名称": "网点名称", "实收金额": "成交金额"})
    renamed = tmp_path / "销售明细_新列名.xlsx"
    with pd.ExcelWriter(renamed) as w:
        for k, v in book.items():
            v.to_excel(w, sheet_name=k, index=False)
    files = {"sales_file": str(renamed), "minutes_file": str(src / "周会纪要_2026W44.docx")}
    _, state = _run(orch, files)
    assert state == S.WAITING_INPUT  # 改配置前：列名不认识，暂停

    def add(r):
        al = r["admission"]["column_aliases"]
        al["门店"].append("网点名称")
        al["销售额"].append("成交金额")
    edit_employee(add)
    tid, state = _run(orch, files)
    assert state == S.PENDING_REVIEW
    assert "573,635 元" in _text(orch, tid)  # 与原列名的数据结果一致