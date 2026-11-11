"""跨周长期记忆（LangGraph Store）：记住每周已交付周报的要点，下一周撰写时带上，便于跟进。

- 写入：任务交付时，记下该周期的“下周关注事项”、异常对象及原因摘要、纪要行动项（命名空间 = 员工 + delivered，键 = 统计周期）。
  同一周期多次交付时以最后一次为准。
- 读取：撰写周报时按上一周期取出，作为输入的 last_week 交给模型（岗位说明要求跟进其中仍需关注的事项）。
- 只记录已交付（审核通过）的内容，未通过审核的草稿不会进入记忆。
存储为 runs 目录下的 memory.sqlite（LangGraph SqliteStore），与任务库分开，可单独备份或清空。
"""
from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from typing import Any

from langgraph.store.sqlite import SqliteStore

_lock = threading.Lock()
_stores: dict[str, SqliteStore] = {}


def open_memory(path: Path) -> SqliteStore:
    key = str(path)
    with _lock:
        if key not in _stores:
            path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(key, check_same_thread=False, isolation_level=None, timeout=30)
            conn.execute("PRAGMA journal_mode=WAL")
            st = SqliteStore(conn)
            st.setup()
            _stores[key] = st
        return _stores[key]


def _ns(employee: str) -> tuple[str, ...]:
    return (employee, "delivered")


def remember(store: SqliteStore, employee: str, period: str, value: dict[str, Any]) -> None:
    store.put(_ns(employee), period, value)


def recall(store: SqliteStore, employee: str, period: str | None) -> dict[str, Any] | None:
    if not period:
        return None
    item = store.get(_ns(employee), period)
    return item.value if item else None


def history(store: SqliteStore, employee: str, limit: int = 20) -> list[dict[str, Any]]:
    """已记住的周期（按周期倒序）。"""
    items = store.search(_ns(employee), limit=limit)
    return sorted((i.value for i in items), key=lambda v: v.get("period", ""), reverse=True)


def digest(period: str, period_label: str, tid: str, texts: dict[str, list[str]], content: dict[str, Any],
           kpis: dict[str, Any], action_items: list[dict[str, Any]]) -> dict[str, Any]:
    """从交付版本提取要记住的内容：文字取占位符已替换的终稿，原因取摘要（brief），不带整篇报告。"""
    labels = {a["id"]: a.get("label", "") for a in kpis.get("anomalies", [])}
    return {
        "period": period, "period_label": period_label, "task_id": tid,
        "follow_ups": list(texts.get("next_week") or []),
        "anomalies": [{"label": labels.get(a.get("id"), a.get("id")), "brief": a.get("brief") or a.get("cause", "")}
                      for a in content.get("anomalies", [])],
        "action_items": action_items,
    }
