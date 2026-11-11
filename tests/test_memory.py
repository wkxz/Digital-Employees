"""跨周长期记忆（LangGraph Store）：交付后记住本周要点，下一周撰写时作为 last_week 交给模型。"""
import json

from mini_employee import memory
from mini_employee.llm.mock import MockLLM
from mini_employee.states import S


def _deliver(orch, sample_files, period="2026-W44"):
    tid = orch.create("weekly_report", {"period": period}, sample_files, user="张三")
    assert orch.drive(tid) == S.PENDING_REVIEW
    t = orch.store.get(tid)
    orch.review(tid, "approve", reviewed_sha256=t["artifact"]["sha256"], actor="reviewer", user="李四")
    assert orch.drive(tid) == S.DELIVERED
    return tid


def test_delivery_is_remembered_with_resolved_texts(orch, sample_files):
    tid = _deliver(orch, sample_files)
    v = memory.recall(orch.memory, "weekly_report", "2026-W44")
    assert v["task_id"] == tid and v["follow_ups"], "应记住下周关注事项"
    assert v["anomalies"] and all("{{" not in a["brief"] for a in v["anomalies"]), "原因摘要应替换掉占位符"
    assert orch.store.events(tid, ["memory_saved"])
    assert memory.history(orch.memory, "weekly_report")[0]["period"] == "2026-W44"


def test_pending_or_rejected_drafts_are_not_remembered(orch, sample_files):
    tid = orch.create("weekly_report", {"period": "2026-W44"}, sample_files, user="张三")
    assert orch.drive(tid) == S.PENDING_REVIEW
    assert memory.recall(orch.memory, "weekly_report", "2026-W44") is None


def test_next_week_draft_receives_last_week(orch, sample_files, monkeypatch):
    memory.remember(orch.memory, "weekly_report", "2026-W43",
                    {"period": "2026-W43", "period_label": "上周", "task_id": "T0", "follow_ups": ["跟踪杭州湖滨店复业"],
                     "anomalies": [{"label": "华东区域", "brief": "竞品开业"}], "action_items": []})
    seen = {}
    real = MockLLM._chat

    def spy(self, messages, tools, meta):
        seen.setdefault(meta.get("purpose"), json.loads(messages[1]["content"]))
        return real(self, messages, tools, meta)

    monkeypatch.setattr(MockLLM, "_chat", spy)
    tid = orch.create("weekly_report", {"period": "2026-W44"}, sample_files, user="张三")
    assert orch.drive(tid) == S.PENDING_REVIEW
    assert seen["draft"]["last_week"]["follow_ups"] == ["跟踪杭州湖滨店复业"]
    assert "last_week" not in seen["analysis"]


def test_first_week_has_no_memory(orch, sample_files, monkeypatch):
    seen = {}
    real = MockLLM._chat

    def spy(self, messages, tools, meta):
        seen.setdefault(meta.get("purpose"), json.loads(messages[1]["content"]))
        return real(self, messages, tools, meta)

    monkeypatch.setattr(MockLLM, "_chat", spy)
    tid = orch.create("weekly_report", {"period": "2026-W44"}, sample_files, user="张三")
    orch.drive(tid)
    assert seen["draft"]["last_week"] is None
