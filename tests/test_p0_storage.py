"""P0 FSStorage tests."""
from __future__ import annotations

import json

from mini_cc.storage import CronJob, FSStorage, Task


def test_messages_roundtrip(tmp_path):
    s = FSStorage(tmp_path / "state")
    msgs = [{"role": "user", "content": "hi"},
            {"role": "assistant", "content": [{"type": "text", "text": "hello"}]}]
    s.save_messages("proj-a", "sess1", msgs)
    assert s.load_messages("proj-a", "sess1") == msgs


def test_messages_isolated_per_project(tmp_path):
    s = FSStorage(tmp_path / "state")
    s.save_messages("proj-a", "sess1", [{"role": "user", "content": "a"}])
    s.save_messages("proj-b", "sess1", [{"role": "user", "content": "b"}])
    assert s.load_messages("proj-a", "sess1")[0]["content"] == "a"
    assert s.load_messages("proj-b", "sess1")[0]["content"] == "b"


def test_todos_roundtrip(tmp_path):
    s = FSStorage(tmp_path / "state")
    todos = [{"content": "do x", "status": "pending"}]
    s.save_todos("proj-a", "sess1", todos)
    assert s.load_todos("proj-a", "sess1") == todos


def test_task_crud(tmp_path):
    s = FSStorage(tmp_path / "state")
    t = Task(id="task_1", subject="x", description="", status="pending",
             owner=None, blockedBy=[])
    s.save_task("proj-a", t)
    loaded = s.load_tasks("proj-a")
    assert len(loaded) == 1
    assert loaded[0].id == "task_1"
    s.delete_task("proj-a", "task_1")
    assert s.load_tasks("proj-a") == []


def test_memory_append(tmp_path):
    s = FSStorage(tmp_path / "state")
    s.append_memory("proj-a", "first")
    s.append_memory("proj-a", "second")
    text = s.load_memory("proj-a")
    assert "first" in text
    assert "second" in text


def test_cron_roundtrip(tmp_path):
    s = FSStorage(tmp_path / "state")
    jobs = [CronJob(job_id="j1", cron="0 9 * * *", prompt="check",
                    recurring=True, durable=True)]
    s.save_cron("proj-a", jobs)
    loaded = s.load_cron("proj-a")
    assert len(loaded) == 1
    assert loaded[0].job_id == "j1"


def test_tool_result_cache(tmp_path):
    s = FSStorage(tmp_path / "state")
    s.write_tool_result("proj-a", "tu_1", "x" * 10000)
    assert s.read_tool_result("proj-a", "tu_1").startswith("x")
    assert s.read_tool_result("proj-a", "tu_missing") is None
