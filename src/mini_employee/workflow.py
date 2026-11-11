"""任务编排图（LangGraph）。

  START ─路由→ created / admission / running / rework / qc / auto_fix   （程序自动推进的节点）
                 ↓ 每个节点结束后，路由函数读取 SQLite 中的当前状态，决定下一个节点
           wait_input / suspended / review                              （等人处理：interrupt 暂停）
                 ↓ 人工操作（补充材料、重试、审核、人工修改……）先经 Store.transition() 改状态，
                   再用 Command(resume=…) 唤醒图，路由按新状态继续
           END（已交付 / 已拒绝 / 已取消）

running / rework 节点内部再运行一张步骤图：每个工作步骤一个节点，边来自员工配置中步骤的 after（依赖），
没有依赖关系的步骤并行执行（如“读取销售明细”与“读取周会纪要”）；全部分支汇合到 finish 节点，
由它统一生成产物并流转，或在有步骤失败时统一暂停。

设计约束：
- SQLite 任务表是唯一的状态来源。图状态只保存 task_id 等运行信息；节点内的状态变更全部经过
  Store.transition()（查表 → 触发方 → 闸门 → 乐观锁 → 写事件），图本身不改业务状态。
- 路由是纯程序函数，不交给模型。
- LangGraph 检查点只记录“图停在哪个节点”。检查点丢失或与 SQLite 不一致时，从 START 按 SQLite 状态重新路由；
  节点依赖每步落盘的结果与输入哈希，重复执行不会重复调用模型或重复写文件。
"""
from __future__ import annotations

import operator
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, TypedDict

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.errors import GraphRecursionError
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from .states import AUTO, TRANSITIONS, S

if TYPE_CHECKING:
    from .orchestrator import Orchestrator

MAX_HOPS = 200  # 与原状态推进循环的上限一致

AUTO_NODES = {S.CREATED: "created", S.ADMISSION: "admission", S.RUNNING: "running", S.REWORK: "rework",
              S.QC: "qc", S.AUTO_FIXING: "auto_fix"}
WAIT_NODES = {S.WAITING_INPUT: "wait_input", S.SUSPENDED: "suspended", S.PENDING_REVIEW: "review"}
NODES = [*AUTO_NODES.values(), *WAIT_NODES.values()]


class TaskState(TypedDict, total=False):
    task_id: str
    resumed: bool  # 本次运行是否为断点恢复（进程中断后 mini resume）
    state: str     # 最近一个节点结束时的业务状态，仅用于调试与可视化，以 SQLite 为准


def route_for(state: S | str) -> str:
    st = S(state)
    return AUTO_NODES.get(st) or WAIT_NODES.get(st) or END


def exits(state: S) -> list[str]:
    """由状态流转表推出某个节点结束后可能去往的节点：图的边与状态机一一对应。
    路由函数返回表外的节点时 LangGraph 直接报错，不会悄悄走到别处。"""
    out = {route_for(state)}  # 留在原节点：自动节点本轮未推进 / 等人节点被唤醒但状态未变
    for t in TRANSITIONS:
        if t.src != state:
            continue
        if t.dst is None:  # 回到暂停前的状态（断点）：可能是任何自动推进的状态
            out |= {route_for(s) for s in AUTO}
        else:
            out.add(route_for(t.dst))
    return sorted(out)


def build_task_graph(orch: "Orchestrator", checkpointer: Any | None = None):
    def current(tid: str) -> S:
        return S(orch.store.get(tid)["state"])

    def route(st: TaskState) -> str:
        return route_for(current(st["task_id"]))

    def auto_node(name: str):
        def node(st: TaskState) -> TaskState:
            tid = st["task_id"]
            orch.advance(tid, resumed=bool(st.get("resumed")))
            return {"resumed": False, "state": current(tid).value}
        node.__name__ = name
        return node

    def wait_node(name: str):
        def node(st: TaskState) -> TaskState:
            tid = st["task_id"]
            task = orch.store.get(tid)
            # 暂停，等人处理；人工操作已先经 Store.transition() 改变状态，唤醒后由路由按新状态继续
            interrupt({"task_id": tid, "state": S(task["state"]).value, "reason": task.get("pause_reason")})
            return {"state": current(tid).value}
        node.__name__ = name
        return node

    g = StateGraph(TaskState)
    for name in AUTO_NODES.values():
        g.add_node(name, auto_node(name))
    for name in WAIT_NODES.values():
        g.add_node(name, wait_node(name))
    g.add_conditional_edges(START, route, [*NODES, END])  # 从 SQLite 中的任意状态开始或恢复
    for st, name in {**AUTO_NODES, **WAIT_NODES}.items():
        g.add_conditional_edges(name, route, exits(st))
    return g.compile(checkpointer=checkpointer, name="weekly_report_task")


def open_checkpointer(path: Path) -> SqliteSaver:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), check_same_thread=False, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    saver = SqliteSaver(conn)
    saver.setup()
    return saver


def run_task(graph: Any, tid: str, resumed: bool = False) -> None:
    """推进任务直到等人处理或结束。图停在等人节点时唤醒它，否则从 START 按 SQLite 状态路由。"""
    cfg = {"configurable": {"thread_id": tid}, "recursion_limit": MAX_HOPS}
    snap = graph.get_state(cfg)
    waiting = any(t.interrupts for t in snap.tasks) if snap and snap.next else False
    try:
        if waiting:
            graph.invoke(Command(resume={"task_id": tid}), cfg)
        else:
            graph.invoke({"task_id": tid, "resumed": resumed}, cfg)
    except GraphRecursionError as e:
        raise RuntimeError("状态推进次数过多，疑似循环") from e


def mermaid(graph: Any) -> str:
    return graph.get_graph().draw_mermaid()


# ================================================================ 步骤图（running / rework 节点内部）

def step_deps(steps: list[dict[str, Any]]) -> dict[str, list[str]]:
    """步骤依赖：配置了 after 的按 after，否则依赖上一步（顺序执行，兼容旧配置）。"""
    out: dict[str, list[str]] = {}
    prev = None
    for s in steps:
        out[s["id"]] = list(s["after"]) if "after" in s else ([prev] if prev else [])
        prev = s["id"]
    return out


def ancestors(deps: dict[str, list[str]]) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for sid in deps:  # deps 只能指向前面的步骤（配置校验保证），按顺序计算即可
        out[sid] = set(deps[sid]).union(*(out[d] for d in deps[sid])) if deps[sid] else set()
    return out


@dataclass
class StepRun:
    """一次执行 / 返工的上下文：本次运行不变的信息，供各步骤节点使用。"""
    task: dict[str, Any]
    emp: Any
    fb: dict[str, Any] | None
    resumed: bool
    rerun_sections: set[str]
    ih: dict[str, str] = field(default_factory=dict)  # 每个步骤的输入哈希
    deps: dict[str, list[str]] = field(default_factory=dict)
    ancestors: dict[str, set[str]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.deps = step_deps(self.emp.steps)
        self.ancestors = ancestors(self.deps)


def _first(a: Any, b: Any) -> Any:
    return a or b


class StepsState(TypedDict, total=False):
    reran: Annotated[list[str], operator.add]   # 本次实际重新执行的按版本步骤（下游据此判断是否受影响）
    failure: Annotated[dict[str, Any] | None, _first]  # 第一个失败的步骤；之后的步骤不再执行


def build_steps_graph(steps: list[dict[str, Any]], run_one: Any, finish: Any):
    """由步骤配置构建步骤图。run_one(step, reran) 执行一个步骤，finish(failure) 汇合收尾。"""
    deps = step_deps(steps)
    g = StateGraph(StepsState)

    def step_node(step: dict[str, Any]):
        def node(st: StepsState) -> StepsState:
            if st.get("failure"):  # 前面已有步骤失败：跳过，由 finish 统一暂停
                return {}
            return run_one(step, set(st.get("reran") or []))
        return node

    for s in steps:
        g.add_node(s["id"], step_node(s))
    g.add_node("finish", lambda st: finish(st.get("failure")) or {})
    used = {d for ds in deps.values() for d in ds}
    for sid, ds in deps.items():
        if not ds:
            g.add_edge(START, sid)
        else:
            g.add_edge(ds if len(ds) > 1 else ds[0], sid)  # 多个依赖：全部完成后才执行
    sinks = [sid for sid in deps if sid not in used]
    g.add_edge(sinks if len(sinks) > 1 else sinks[0], "finish")
    g.add_edge("finish", END)
    return g.compile(name="steps")


def run_steps(orch: "Orchestrator", run: StepRun) -> None:
    graph = build_steps_graph(run.emp.steps, lambda step, reran: orch.run_step(run, step, reran),
                              lambda failure: orch.finish_steps(run, failure))
    graph.invoke({"reran": [], "failure": None}, {"recursion_limit": len(run.emp.steps) * 2 + 5})
