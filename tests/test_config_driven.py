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


def test_unsupported_format_pauses_with_clear_message(edit_employee, orch, sample_files):
    edit_employee(lambda r: r["report"].__setitem__("output_formats", ["docx", "pptx"]))
    tid, state = _run(orch, sample_files)
    assert state == S.SUSPENDED and "不支持的交付格式 pptx" in orch.store.get(tid)["pause_reason"]
