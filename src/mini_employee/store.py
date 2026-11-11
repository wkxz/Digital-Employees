"""SQLite 存储：任务表 + 只追加的事件表（事件溯源，支撑断点恢复与回放）。"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import runs_dir
from .states import S, TERMINAL, find

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
  id TEXT PRIMARY KEY, employee TEXT NOT NULL, state TEXT NOT NULL,
  form_json TEXT NOT NULL, meta_json TEXT NOT NULL DEFAULT '{}',
  version INTEGER NOT NULL DEFAULT 1,       -- 产物版本：每次退回返工 +1
  qc_rounds INTEGER NOT NULL DEFAULT 0,     -- 当前版本已自动修正的轮数
  rework_rounds INTEGER NOT NULL DEFAULT 0,
  resume_to TEXT, pause_reason TEXT, artifact_json TEXT,
  row_version INTEGER NOT NULL DEFAULT 0,   -- 乐观锁
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
  seq INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL, ts TEXT NOT NULL,
  type TEXT NOT NULL, step_id TEXT, payload_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_events_task ON events(task_id, seq);
CREATE TABLE IF NOT EXISTS live (   -- 运行中的实时进度（当前节点、模型正在输出的内容）：按 key 覆盖写，供网页推送
  task_id TEXT NOT NULL, key TEXT NOT NULL, seq INTEGER NOT NULL, data_json TEXT NOT NULL,
  PRIMARY KEY(task_id, key)
);
CREATE TABLE IF NOT EXISTS config_changes (   -- 员工配置变更记录（只追加）：谁、何时、改了哪个文件、改前改后全文
  seq INTEGER PRIMARY KEY AUTOINCREMENT, employee TEXT NOT NULL, file TEXT NOT NULL, ts TEXT NOT NULL,
  user TEXT NOT NULL, note TEXT NOT NULL DEFAULT '', sha_before TEXT, sha_after TEXT NOT NULL,
  content_before TEXT, content_after TEXT NOT NULL
);
"""


class IllegalTransition(Exception):
    pass


class ConcurrentUpdate(Exception):
    pass


def _now() -> str:
    return datetime.now().isoformat(timespec="milliseconds")


class Store:
    def __init__(self, path: Path | None = None):
        self.path = path or runs_dir() / "tasks.db"
        self.db = sqlite3.connect(self.path, timeout=10, check_same_thread=False, isolation_level=None)
        # 编排图的并行步骤在多个线程中写事件、读任务：同一连接上的操作串行执行，事务不会交错
        self._lock = threading.RLock()
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)

    # ------------------------------------------------------------ tasks
    @staticmethod
    def new_id() -> str:
        return datetime.now().strftime("T%m%d-%H%M%S-") + uuid.uuid4().hex[:4]

    def create_task(self, employee: str, form: dict, meta: dict | None = None, tid: str | None = None) -> str:
        with self._lock:
            tid = tid or self.new_id()
            now = _now()
            self.db.execute("INSERT INTO tasks(id, employee, state, form_json, meta_json, created_at, updated_at) "
                            "VALUES (?,?,?,?,?,?,?)",
                            (tid, employee, S.CREATED, json.dumps(form, ensure_ascii=False),
                             json.dumps(meta or {}, ensure_ascii=False), now, now))
            self.event(tid, "task_created", None, {"employee": employee, "form": form})
            return tid

    def get(self, tid: str) -> dict[str, Any]:
        with self._lock:
            row = self.db.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
            if not row:
                raise KeyError(tid)
            t = dict(row)
            t["state"] = S(t["state"])
            t["form"] = json.loads(t.pop("form_json"))
            t["meta"] = json.loads(t.pop("meta_json"))
            t["artifact"] = json.loads(t.pop("artifact_json") or "null")
            return t

    def list(self) -> list[dict[str, Any]]:
        with self._lock:
            ids = [r[0] for r in self.db.execute("SELECT id FROM tasks ORDER BY created_at DESC")]
            return [self.get(i) for i in ids]

    def update(self, tid: str, expect_row_version: int | None = None, **fields: Any) -> None:
        with self._lock:
            cols, vals = [], []
            for k, v in fields.items():
                if k in ("form", "meta", "artifact"):
                    k, v = f"{k}_json", json.dumps(v, ensure_ascii=False)
                cols.append(f"{k}=?")
                vals.append(v.value if isinstance(v, S) else v)
            sql = f"UPDATE tasks SET {', '.join(cols)}, row_version=row_version+1, updated_at=? WHERE id=?"
            vals += [_now(), tid]
            if expect_row_version is not None:
                sql += " AND row_version=?"
                vals.append(expect_row_version)
            cur = self.db.execute(sql, vals)
            if cur.rowcount != 1:
                raise ConcurrentUpdate(f"任务 {tid} 已被其他操作修改，请刷新后重试")

    # ------------------------------------------------------------ events
    def event(self, tid: str, type_: str, step_id: str | None, payload: dict[str, Any]) -> int:
        with self._lock:
            cur = self.db.execute("INSERT INTO events(task_id, ts, type, step_id, payload_json) VALUES (?,?,?,?,?)",
                                  (tid, _now(), type_, step_id, json.dumps(payload, ensure_ascii=False, default=str)))
            return int(cur.lastrowid)

    def events(self, tid: str, types: list[str] | None = None) -> list[dict[str, Any]]:
        with self._lock:
            sql, args = "SELECT * FROM events WHERE task_id=?", [tid]
            if types:
                sql += f" AND type IN ({','.join('?' * len(types))})"
                args += types
            rows = self.db.execute(sql + " ORDER BY seq", args).fetchall()
            return [{**dict(r), "payload": json.loads(r["payload_json"])} for r in rows]

    # ------------------------------------------------------------ live progress
    def live_set(self, tid: str, key: str, data: dict[str, Any]) -> None:
        """写一条实时进度（同一 key 覆盖）。seq 取微秒时间戳并保证递增（清空后也不回退），读取方据此只取变化的部分。"""
        with self._lock:
            self.db.execute("INSERT OR REPLACE INTO live(task_id, key, seq, data_json) "
                            "VALUES (?, ?, MAX(?, (SELECT COALESCE(MAX(seq), 0) + 1 FROM live)), ?)",
                            (tid, key, time.time_ns() // 1000, json.dumps(data, ensure_ascii=False, default=str)))

    def live(self, tid: str, after: int = 0) -> list[dict[str, Any]]:
        with self._lock:
            rows = self.db.execute("SELECT key, seq, data_json FROM live WHERE task_id=? AND seq>? ORDER BY seq",
                                   (tid, after)).fetchall()
            return [{"key": r["key"], "seq": r["seq"], "data": json.loads(r["data_json"])} for r in rows]

    def live_clear(self, tid: str) -> None:
        with self._lock:
            self.db.execute("DELETE FROM live WHERE task_id=?", (tid,))

    # ------------------------------------------------------------ config changes
    def config_change(self, employee: str, file: str, user: str, before: str | None, after: str,
                      note: str = "") -> int:
        from .config_admin import _sha
        cur = self.db.execute(
            "INSERT INTO config_changes(employee, file, ts, user, note, sha_before, sha_after, content_before, "
            "content_after) VALUES (?,?,?,?,?,?,?,?,?)",
            (employee, file, _now(), user, note, _sha(before), _sha(after), before, after))
        return int(cur.lastrowid)

    def config_changes(self, employee: str, limit: int = 50) -> list[dict[str, Any]]:
        rows = self.db.execute("SELECT * FROM config_changes WHERE employee=? ORDER BY seq DESC LIMIT ?",
                               (employee, limit)).fetchall()
        return [dict(r) for r in rows]

    def last_config_change(self, employee: str, file: str) -> dict[str, Any] | None:
        r = self.db.execute("SELECT * FROM config_changes WHERE employee=? AND file=? ORDER BY seq DESC LIMIT 1",
                            (employee, file)).fetchone()
        return dict(r) if r else None

    def get_config_change(self, seq: int) -> dict[str, Any] | None:
        r = self.db.execute("SELECT * FROM config_changes WHERE seq=?", (seq,)).fetchone()
        return dict(r) if r else None

    # ------------------------------------------------------------ state machine
    def reject(self, tid: str, event: str, actor: str, reason: str, state: S | None = None) -> None:
        """拒绝一次流转：留痕后抛出 IllegalTransition。"""
        self.event(tid, "transition_rejected", None, {"from": state or self.get(tid)["state"], "event": event,
                                                      "actor": actor, "reason": reason})
        raise IllegalTransition(reason)

    def transition(self, tid: str, event: str, actor: str, ctx: dict[str, Any] | None = None,
                   **updates: Any) -> S:
        """唯一的状态变更入口：查流转表 → 校验触发方 → 执行闸门 → 乐观锁更新 → 记事件。"""
        with self._lock:
            ctx = ctx or {}
            task = self.get(tid)
            t = find(task["state"], event)
            reason = None
            if t is None:
                reason = f"非法流转：{task['state']} 状态下不允许事件 {event}"
            elif actor not in t.actors:
                reason = f"无权限：{event} 只能由 {'/'.join(t.actors)} 触发，当前为 {actor}"
            elif t.guard and (g := t.guard(task, ctx)):
                reason = f"闸门未通过：{g}"
            if reason:
                self.reject(tid, event, actor, reason, task["state"])
            dst = t.dst or S(task["resume_to"])
            if dst not in (S.SUSPENDED, S.WAITING_INPUT) and "pause_reason" not in updates:
                updates["pause_reason"] = None
            # 状态与对应事件在同一事务内写入：要么都成功，要么都不生效，不会出现“状态变了却没有留痕”
            self.db.execute("BEGIN IMMEDIATE")
            try:
                self.update(tid, expect_row_version=task["row_version"], state=dst, **updates)
                self.event(tid, "state_changed", None, {"from": task["state"], "to": dst, "event": event, "actor": actor,
                                                        "user": ctx.get("user"), "reason": ctx.get("reason")})
            except BaseException:
                self.db.execute("ROLLBACK")
                raise
            self.db.execute("COMMIT")
            return dst

    def unfinished(self) -> list[dict[str, Any]]:
        return [t for t in self.list() if t["state"] not in TERMINAL]
