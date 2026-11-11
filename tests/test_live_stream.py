"""实时推送：执行任务时节点进度与模型输出写入实时进度表，网页经 SSE 读取变化推给浏览器。"""
import importlib.util
import json
import threading
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from mini_employee.states import S

from .conftest import ROOT


@pytest.fixture
def web():
    spec = importlib.util.spec_from_file_location("web_server", ROOT / "app" / "web_server.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _frames(raw: str) -> list[tuple[str, dict, str | None]]:
    out = []
    for block in raw.strip().split("\n\n"):
        ev, data, id_ = None, None, None
        for line in block.split("\n"):
            if line.startswith("event: "):
                ev = line[7:]
            elif line.startswith("data: "):
                data = json.loads(line[6:])
            elif line.startswith("id: "):
                id_ = line[4:]
        if ev:
            out.append((ev, data, id_))
    return out


def test_sse_frames_push_live_rows_then_only_changes(web, sample_files):
    o = web.orch()
    tid = o.create("weekly_report", {"period": "2026-W44"}, sample_files, user="张三")
    assert o.drive(tid) == S.PENDING_REVIEW
    frames, ev, lv, rv, ended = web.sse_frames(tid, 0, 0, -1)
    kinds = [json.loads(f.split("data: ", 1)[1])["key"] for f in frames if "event: live" in f]
    assert {"nodes", "model:analysis", "model:draft"} <= set(kinds)
    assert "event: tick" in frames[-1] and not ended
    again, *_ = web.sse_frames(tid, ev, lv, rv)
    assert again == [], "没有变化时不应重复推送"
    o.store.live_set(tid, "nodes", {"running": ["qc"]})
    o.store.event(tid, "note", None, {})
    again, *_ = web.sse_frames(tid, ev, lv, rv)
    assert [f.split("\n")[1] for f in again] == ["event: live", "event: tick"]


def test_sse_endpoint_streams_and_ends_for_finished_task(web, sample_files):
    o = web.orch()
    tid = o.create("weekly_report", {"period": "2026-W44"}, sample_files, user="张三")
    o.drive(tid)
    assert o.cancel(tid) == S.CANCELLED
    srv = ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{srv.server_address[1]}/api/tasks/{tid}/stream"
        with urllib.request.urlopen(url, timeout=10) as r:
            assert r.headers["Content-Type"].startswith("text/event-stream")
            frames = _frames(r.read().decode("utf-8"))
        events = [f[0] for f in frames]
        assert events[-1] == "end" and "tick" in events and "live" in events
        tick = next(f for f in frames if f[0] == "tick")
        assert tick[1]["state"] == "CANCELLED" and tick[2]  # 带 id，断线重连时浏览器据此续传
        req = urllib.request.Request(url, headers={"Last-Event-ID": tick[2]})
        with urllib.request.urlopen(req, timeout=10) as r:  # 从上次位置续传：不重复推送已收到的进度
            assert [f[0] for f in _frames(r.read().decode("utf-8"))] == ["tick", "end"]
    finally:
        srv.shutdown()
