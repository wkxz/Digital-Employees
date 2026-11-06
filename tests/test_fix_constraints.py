"""自动修正时把其他质检要求一并交给模型（must_keep），避免修一处、坏一处：
实测中压缩篇幅时删掉了摘要里的“目标达成率”，引出新的不通过，修正轮次用尽后暂停转人工。"""
from mini_employee.config import load_employee


def test_must_keep_lists_other_requirements(orch, sample_files):
    emp = load_employee("weekly_report")
    tid = orch.create("weekly_report", {"period": "2026-W44"}, sample_files)
    keep = orch._must_keep(list(emp.quality_checks), orch.store.get(tid))
    assert "summary 章节必须提到：实收、环比、毛利率、目标达成率" in keep
    assert any("不得出现：客单价、同比" in k for k in keep)
    assert any("不能为压缩篇幅删掉异常条目" in k for k in keep)


def test_fix_step_receives_must_keep(orch, sample_files, monkeypatch):
    import mini_employee.orchestrator as O
    seen = {}
    real = O.run_agent_step

    def spy(*a, **kw):
        if kw.get("purpose") == "fix":
            seen["payload"] = kw["payload"]
        return real(*a, **kw)

    monkeypatch.setattr(O, "run_agent_step", spy)
    # 强制第一轮质检出现程序修不了的问题：摘要里删掉“目标达成率”
    tid = orch.create("weekly_report", {"period": "2026-W44"}, sample_files)
    orig = O.Orchestrator._render

    def render(self, task, emp, gw, content, version, tag, purpose):
        if purpose != "fix":
            content = {**content, "summary": [s.replace("目标达成率", "目标") for s in content.get("summary", [])]}
        return orig(self, task, emp, gw, content, version, tag, purpose)

    monkeypatch.setattr(O.Orchestrator, "_render", render)
    orch.drive(tid)
    assert "summary 章节必须提到：实收、环比、毛利率、目标达成率" in seen["payload"]["must_keep"]
