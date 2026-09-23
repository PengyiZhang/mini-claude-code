"""F3.3 /fork — branch a session by copying its transcript."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "mini_cc"))

from mini_cc.commands.registry import CommandContext  # noqa: E402
from mini_cc.commands.builtin.session_cmds import _cmd_fork  # noqa: E402


class _FakeStorage:
    def __init__(self):
        self.messages: dict[str, list[dict]] = {}

    def load_messages(self, pid, sid):
        return list(self.messages.get(sid, []))

    def save_messages(self, pid, sid, msgs):
        self.messages[sid] = list(msgs)


class _FakeSM:
    def __init__(self):
        self.warmed: list[tuple[str, str]] = []

    def start_session(self, pid, sid, **kw):
        self.warmed.append((pid, sid))
        return object()


def _run(cmd):
    return [e for e in _cmd_fork(cmd)]


def _ctx(sid="sess_src", storage=None, sm=None):
    return CommandContext(
        project_id="p1", session_id=sid, tenant_id="t1",
        args="", project=None, session_manager=sm, storage=storage,
    )


def test_fork_copies_messages_and_switches_session():
    storage = _FakeStorage()
    storage.messages["sess_src"] = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": [{"type": "text", "text": "hello"}]},
    ]
    sm = _FakeSM()
    events = _run(_ctx(storage=storage, sm=sm))

    resumed = [e for e in events if e.get("type") == "session_resumed"]
    assert len(resumed) == 1, events
    new_sid = resumed[0]["session_id"]
    assert new_sid != "sess_src"
    assert new_sid.startswith("sess_")
    assert storage.messages[new_sid] == storage.messages["sess_src"]
    assert sm.warmed == [("p1", new_sid)]
    # Original untouched
    assert "sess_src" in storage.messages
    text = next(e for e in events if e.get("type") == "text")
    assert "🌱" in text["text"] and "sess_src" in text["text"] and new_sid in text["text"]


def test_fork_empty_session_reports_nothing_to_do():
    storage = _FakeStorage()
    storage.messages["sess_src"] = []
    sm = _FakeSM()
    events = _run(_ctx(storage=storage, sm=sm))
    assert all(e.get("type") != "session_resumed" for e in events)
    text = next(e for e in events if e.get("type") == "text")
    assert "nothing to fork" in text["text"].lower()
    assert sm.warmed == []


def test_fork_missing_storage_errors():
    events = _run(_ctx(storage=None, sm=None))
    err = next(e for e in events if e.get("type") == "error")
    assert "unavailable" in err["message"]
