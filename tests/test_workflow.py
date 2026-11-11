"""LangGraph 编排：等人处理时图停在 interrupt；人工操作后唤醒继续；检查点丢失时按 SQLite 状态恢复；
模型步骤子图受轮次上限约束，输出不合规时要求重写。"""

import pytest

from mini_employee.llm.agent import StepLimitExceeded, run_agent_step
from mini_employee.llm.base import LLMResponse, ToolCall
from mini_employee.states import S
from mini_employee.workflow import NODES, WAIT_NODES, mermaid, route_for


def _cfg(tid):
    return {"configurable": {"thread_id": tid}}


def _interrupted_at(orch, tid):
    snap = orch.graph.get_state(_cfg(tid))
    return [t.name for t in snap.tasks if t.interrupts]


def test_routes_cover_every_state():
    for st in S:
        r = route_for(st)
        assert r in NODES or r == "__end__"
    assert route_for(S.DELIVERED) == route_for(S.CANCELLED) == "__end__"


def test_graph_pauses_at_review_and_resumes_after_approval(orch, sample_files):
    tid = orch.create("weekly_report", {"period": "2026-W44"}, sample_files, user="张三")
    assert orch.drive(tid) == S.PENDING_REVIEW
    assert _interrupted_at(orch, tid) == [WAIT_NODES[S.PENDING_REVIEW]], "待审核时图应停在 review 节点"
    t = orch.store.get(tid)
    orch.review(tid, "approve", reviewed_sha256=t["artifact"]["sha256"], actor="reviewer", user="李四")
    assert orch.drive(tid) == S.DELIVERED
    assert not orch.graph.get_state(_cfg(tid)).next, "交付后图应结束"


def test_missing_input_waits_then_supplement_wakes_graph(orch, sample_files):
    tid = orch.create("weekly_report", {"period": "2026-W44"}, {"sales_file": sample_files["sales_file"]}, user="张三")
    assert orch.drive(tid) == S.WAITING_INPUT
    assert _interrupted_at(orch, tid) == [WAIT_NODES[S.WAITING_INPUT]]
    assert orch.supplement(tid, {"minutes_file": sample_files["minutes_file"]}) == S.PENDING_REVIEW
    assert _interrupted_at(orch, tid) == [WAIT_NODES[S.PENDING_REVIEW]]


def test_lost_checkpoint_falls_back_to_sqlite_state(orch, sample_files, tmp_path):
    """检查点只记录图的位置：换一个空的检查点库，仍按 SQLite 中的状态继续，已完成的步骤不重跑。"""
    from mini_employee.orchestrator import Orchestrator
    from mini_employee.workflow import build_task_graph, open_checkpointer

    tid = orch.create("weekly_report", {"period": "2026-W44"}, sample_files, user="张三")
    orch.advance(tid)  # 只推进两个节点就“中断”：已创建 → 准入校验 → 执行中
    orch.advance(tid)
    assert orch.store.get(tid)["state"] == S.RUNNING
    fresh = Orchestrator(orch.store)
    fresh._graph = build_task_graph(fresh, open_checkpointer(tmp_path / "empty.sqlite"))
    assert fresh.drive(tid, resumed=True) == S.PENDING_REVIEW
    calls = [e for e in orch.store.events(tid, ["model_called"])]
    assert calls, "应正常执行模型步骤"


def test_mermaid_export_lists_nodes(orch):
    text = mermaid(orch.graph)
    for name in NODES:
        assert name in text


# ---------------------------------------------------------------- 模型步骤子图

class _Gateway:
    def __init__(self):
        self.calls = []

    def model_tools(self, tools):
        return [{"type": "function", "function": {"name": t, "parameters": {"type": "object"}}} for t in tools]

    def call(self, name, args, **kw):
        self.calls.append((name, args))
        return {"ok": True}


class _ScriptedLLM:
    name = "scripted"

    def __init__(self, replies):
        self.replies = list(replies)
        self.seen = []

    def chat(self, messages, tools, meta):
        self.seen.append((list(messages), list(tools), meta))
        return self.replies.pop(0)


SCHEMA = {"type": "object", "required": ["answer"], "properties": {"answer": {"type": "string"}}}


def _run(llm, gw, max_turns=4):
    events = []
    out = run_agent_step(llm, gw, lambda t, s, p: events.append((t, p)), step_id="analyze", purpose="analysis",
                         system="sys", payload={"q": 1}, tools=["search_minutes"], output_schema=SCHEMA,
                         max_turns=max_turns, max_seconds=60)
    return out, events


def test_agent_graph_calls_tools_then_returns_valid_json():
    llm = _ScriptedLLM([LLMResponse(None, [ToolCall("c1", "search_minutes", {"q": "华东"})]),
                        LLMResponse('{"answer": "ok"}')])
    gw = _Gateway()
    out, events = _run(llm, gw)
    assert out == {"answer": "ok"}
    assert gw.calls == [("search_minutes", {"q": "华东"})]
    assert [p["turn"] for t, p in events if t == "model_called"] == [1, 2]
    assert llm.seen[1][0][-1]["role"] == "tool", "工具结果应回传给模型"


def test_agent_graph_asks_for_rewrite_on_invalid_output():
    llm = _ScriptedLLM([LLMResponse("不是 JSON"), LLMResponse('{"answer": "ok"}')])
    out, events = _run(llm, _Gateway())
    assert out == {"answer": "ok"}
    assert any(t == "model_output_invalid" for t, p in events)
    assert "输出不符合要求" in llm.seen[1][0][-1]["content"]


def test_agent_graph_enforces_turn_limit_and_wraps_up():
    llm = _ScriptedLLM([LLMResponse(None, [ToolCall(f"c{i}", "search_minutes", {})]) for i in range(3)]
                       + [LLMResponse("仍然不是 JSON")])
    with pytest.raises(StepLimitExceeded):
        _run(llm, _Gateway(), max_turns=4)
    last_messages, last_tools, meta = llm.seen[-1]
    assert meta["wrap_up"] and last_tools == [], "最后一轮应收回工具、要求直接给出结果"


# ---------------------------------------------------------------- 步骤图：按依赖并行

def _steps():
    from mini_employee.config import load_employee
    return load_employee("weekly_report").steps


def test_step_graph_runs_independent_steps_in_parallel():
    from mini_employee.workflow import build_steps_graph
    g = build_steps_graph(_steps(), lambda s, r: {}, lambda f: None).get_graph()
    edges = {(e.source, e.target) for e in g.edges}
    assert {("__start__", "load"), ("__start__", "minutes")} <= edges, "读取销售明细与读取周会纪要应并行"
    assert ("kpi", "charts") in edges and ("kpi", "analysis") in edges, "生成图表与异常原因分析应并行"
    assert ("charts", "render") in edges and ("draft", "render") in edges
    assert ("render", "finish") in edges


def test_step_graph_skips_after_failure_and_reports_once():
    from mini_employee.workflow import build_steps_graph
    ran, finished = [], []

    def run_one(step, reran):
        ran.append(step["id"])
        return {"failure": {"idx": 2, "msg": "x"}} if step["id"] == "kpi" else {}

    build_steps_graph(_steps(), run_one, finished.append).invoke({"reran": [], "failure": None})
    assert "analysis" not in ran and "render" not in ran, "有步骤失败后，下游步骤不再执行"
    assert finished == [{"idx": 2, "msg": "x"}], "由汇合节点统一处理一次"


def test_steps_without_after_stay_sequential():
    from mini_employee.workflow import step_deps
    deps = step_deps([{"id": "a"}, {"id": "b"}, {"id": "c", "after": ["a"]}])
    assert deps == {"a": [], "b": ["a"], "c": ["a"]}


def test_config_rejects_bad_step_dependencies():
    import copy

    from mini_employee.config import load_employee
    from mini_employee.validate import _check_step_order
    steps = copy.deepcopy(load_employee("weekly_report").raw["steps"])
    errs = []
    analysis = next(s for s in steps if s["id"] == "analysis")
    analysis["after"] = ["kpi"]  # 漏了周会纪要
    _check_step_order(steps, errs.append)
    assert any("minutes" in e for e in errs)
    errs.clear()
    analysis["after"] = ["draft"]  # 引用后面的步骤
    _check_step_order(steps, errs.append)
    assert any("只能引用写在它前面的步骤" in e for e in errs)


def test_supplement_sales_does_not_rerun_minutes(orch, sample_files):
    tid = orch.create("weekly_report", {"period": "2026-W44"}, sample_files, user="张三")
    orch.drive(tid)
    t = orch.store.get(tid)
    from mini_employee.workflow import StepRun
    run = StepRun(task=t, emp=orch._emp(t), fb=None, resumed=False, rerun_sections=set())
    before = orch._step_hashes(t, run.emp, run.ancestors)
    t2 = {**t, "form": {**t["form"], "sales_file": ["inputs/另一个文件.xlsx"]}}
    after = orch._step_hashes(t2, run.emp, run.ancestors)
    assert before["minutes"] == after["minutes"], "周会纪要步骤不依赖销售明细"
    assert before["load"] != after["load"] and before["kpi"] != after["kpi"]
