"""P0-9 + P1-2: cancellation/stream-error must persist the partial
assistant content already streamed to the client.

Before the fix, the cancelled branch did `yield done; _persist(); return`
without appending the streamed text / tool_use blocks to self.messages —
so the transcript lost everything the model had generated before the
client clicked stop. Same shape on stream exceptions (P1-2): the
client saw the text via SSE, but the disk copy was empty, so resume
made the model "forget" what it just said.
"""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "mini_cc"))

from mini_cc.core.loop import AgentLoop, ProjectRef, INTERRUPTED_TOOL_RESULT  # noqa: E402
from mini_cc.core.llm import StreamEvent  # noqa: E402
from mini_cc.sandbox import SubprocessSandbox  # noqa: E402
from mini_cc.storage import FSStorage  # noqa: E402


class _ScriptedProvider:
    """Provider whose stream() yields a programmed event list, optionally
    raising mid-stream or pausing for cancellation."""
    provider_name = "anthropic"

    def __init__(self, events, raise_after=None, pause_at=None,
                 pause_signal: threading.Event | None = None):
        self._events = list(events)
        self._raise_after = raise_after
        self._pause_at = pause_at
        self._pause_signal = pause_signal

    def stream(self, *, model, system, messages, tools, max_tokens):
        for i, ev in enumerate(self._events):
            if self._pause_at == i and self._pause_signal is not None:
                # Wait until the test signals stop() has fired.
                self._pause_signal.wait(timeout=5)
            yield ev
        # raise_after indexes PAST the end of the events list, so the
        # exception fires only after every programmed event has been
        # processed by the consumer (cleaner than interleaving raise
        # with yield mid-iteration).
        if self._raise_after is not None:
            raise RuntimeError("provider stream blew up mid-flight")


def _build_loop(tmp_path: Path, provider):
    sandbox = SubprocessSandbox("proj-cancel-persist", tmp_path / "ws")
    storage = FSStorage(tmp_path / "state")
    ref = ProjectRef(
        project_id="proj-cancel-persist",
        project_root=str(tmp_path / "ws"),
        sandbox=sandbox,
        storage=storage,
        client_factory=lambda: provider,
    )
    loop = AgentLoop(ref, "sess-cancel-persist", system_prompt_override="TEST")
    loop._client = provider  # type: ignore[attr-defined]
    return loop, storage


def _events_text_then_pause():
    """Program a stream that emits text deltas then a phantom ping event
    we can pause on — simulating the model mid-message when the user
    clicks stop. The ping is ignored by the loop body, so the cancel
    check at the top of the next iteration fires when stop() is set."""
    return [
        StreamEvent(kind="text_delta", text="hello "),
        StreamEvent(kind="text_delta", text="world"),
        StreamEvent(kind="ping"),  # sentinel — pause target
    ]


def test_cancel_persists_streamed_text(tmp_path):
    """P0-9: cancellation after the model emitted text deltas must
    persist an assistant message carrying the partial text. Without
    the fix, the transcript ended at the user message and resume made
    the model re-answer from scratch."""
    pause_signal = threading.Event()
    provider = _ScriptedProvider(
        _events_text_then_pause(),
        pause_at=2,  # pause before yielding the ping
        pause_signal=pause_signal,
    )
    loop, storage = _build_loop(tmp_path, provider)

    events: list[dict] = []
    done = threading.Event()

    def run():
        for ev in loop.run("hi"):
            events.append(ev)
        done.set()

    t = threading.Thread(target=run)
    t.start()
    # Wait until the loop has actually entered the stream and emitted
    # both text deltas — at that point the provider is blocked on the
    # pause sentinel. Using a fixed sleep here is racy on slow CI/Windows
    # because the loop may not have reached the for-loop body yet, in
    # which case stop() fires before any delta is processed and the
    # partial buffer is empty.
    deadline = time.time() + 5
    while time.time() < deadline:
        if sum(1 for e in events if e.get("type") == "text") >= 2:
            break
        time.sleep(0.01)
    else:
        t.join(timeout=1)
        raise AssertionError(f"stream never emitted both deltas: {events}")
    # Now the provider is blocked on pause_signal — fire stop, then
    # release the pause so the loop sees _stop.is_set() at the top of
    # the next for-iteration and breaks out.
    loop.stop()
    pause_signal.set()
    assert done.wait(timeout=3), "run did not finish"
    t.join(timeout=5)

    msgs = storage.load_messages(loop.project.project_id, loop.session_id)
    # Find the assistant message with the partial text.
    assistants = [m for m in msgs if m["role"] == "assistant"]
    assert assistants, f"no assistant message persisted: {msgs}"
    last = assistants[-1]
    text_blocks = [b for b in last["content"]
                   if isinstance(b, dict) and b.get("type") == "text"]
    blob = " ".join(b["text"] for b in text_blocks)
    assert "hello" in blob and "world" in blob, blob


def test_exception_persists_streamed_text_and_tool_use(tmp_path):
    """P0-9 (tool_use side): when the provider stream raises AFTER a
    tool_use event has been processed, the partial assistant message
    must carry the tool_use AND a synthetic tool_result must follow so
    the next turn's API call has matched use/result pairs. Verified via
    exception path because the cancel path races with the loop's _stop
    check at the top of each iteration (cancel-before-yield-tool_use is
    the contract)."""
    provider = _ScriptedProvider([
        StreamEvent(kind="text_delta", text="thinking..."),
        StreamEvent(kind="tool_use", tool_call_id="tu_1",
                    tool_name="bash", tool_input={"cmd": "ls"}),
        # raise after the tool_use event is processed
    ], raise_after=1)
    loop, storage = _build_loop(tmp_path, provider)

    for ev in loop.run("hi"):
        pass  # drain

    msgs = storage.load_messages(loop.project.project_id, loop.session_id)
    # Layout: user, assistant(text + tool_use), user(tool_result), assistant([Error]).
    asst = next(m for m in msgs if m["role"] == "assistant"
                and any(isinstance(b, dict) and b.get("type") == "tool_use"
                        for b in m["content"]))
    types = [b.get("type") for b in asst["content"]
             if isinstance(b, dict)]
    assert "text" in types and "tool_use" in types, types
    # Synthetic tool_result follows the partial assistant.
    tu_msg = next(m for m in msgs if m["role"] == "user"
                  and any(isinstance(b, dict)
                          and b.get("type") == "tool_result"
                          for b in m["content"]))
    results = [b for b in tu_msg["content"]
               if isinstance(b, dict) and b.get("type") == "tool_result"]
    assert len(results) == 1
    assert results[0]["tool_use_id"] == "tu_1"
    assert results[0]["content"] == INTERRUPTED_TOOL_RESULT


def test_stream_exception_persists_streamed_text(tmp_path):
    """P1-2: when the provider stream raises after emitting text deltas,
    the partial text must land on disk (not just on the client SSE).
    Otherwise resume shows the model a transcript without the text it
    just produced — incoherent multi-turn continuity."""
    provider = _ScriptedProvider(
        [StreamEvent(kind="text_delta", text="partial "),
         StreamEvent(kind="text_delta", text="answer")],
        raise_after=1,  # blow up after the second delta
    )
    loop, storage = _build_loop(tmp_path, provider)

    events: list[dict] = []
    # Drain fully — error path runs _persist after yield; breaking would
    # skip the persist and mask the bug.
    for ev in loop.run("hi"):
        events.append(ev)

    msgs = storage.load_messages(loop.project.project_id, loop.session_id)
    # After the fix the transcript has: user, assistant(partial text),
    # assistant([Error] note). The partial-text assistant must exist
    # separately from the [Error] one — that's the bug fix.
    assistant_blobs = []
    for m in msgs:
        if m["role"] != "assistant":
            continue
        content = m["content"]
        if not isinstance(content, list):
            continue
        text_blocks = [b for b in content
                       if isinstance(b, dict) and b.get("type") == "text"]
        assistant_blobs.append(" ".join(b["text"] for b in text_blocks))
    joined = " | ".join(assistant_blobs)
    assert "partial" in joined and "answer" in joined, joined
    # [Error] note must be present (existing behavior preserved).
    assert any("[Error]" in b for b in assistant_blobs), joined


def test_normal_path_unaffected(tmp_path):
    """Regression guard: full turn (no cancel, no exception) must still
    behave as before — assistant message persisted, no synthetic
    tool_result appended."""
    provider = _ScriptedProvider([
        StreamEvent(kind="text_delta", text="hello"),
        StreamEvent(
            kind="message_stop", stop_reason="end_turn",
            content_blocks=[{"type": "text", "text": "hello"}],
            usage={"input_tokens": 1, "output_tokens": 1},
        ),
    ])
    loop, storage = _build_loop(tmp_path, provider)
    # Drain to completion — `_persist` runs AFTER the final yield, so a
    # `break` on `done` would skip persistence entirely. (This is the
    # actual production hazard P1-2 / P0-9 are about: SSE consumers
    # abandoning the generator.)
    saw_done = False
    for ev in loop.run("hi"):
        if ev.get("type") == "done":
            saw_done = True
        # no break — keep draining.
    assert saw_done
    msgs = storage.load_messages(loop.project.project_id, loop.session_id)
    assert msgs[-1]["role"] == "assistant"
