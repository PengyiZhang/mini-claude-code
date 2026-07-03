"""Phase I.A: teammate→teammate messages with msg_type in
{result, milestone, blocker} auto-CC lead so the lead's mailbox
sees them without the teammate explicitly addressing lead."""
from mini_cc.teams import MessageBus


def test_result_message_between_teammates_cc_to_lead(tmp_path):
    bus = MessageBus(tmp_path / "ws")
    bus.send("alice", "bob", "task done", msg_type="result")
    assert any(m["content"] == "task done"
               for m in bus.peek_inbox("lead"))


def test_milestone_message_cc_to_lead(tmp_path):
    bus = MessageBus(tmp_path / "ws")
    bus.send("alice", "bob", "milestone hit", msg_type="milestone")
    assert bus.peek_inbox("lead"), "milestone must CC lead"


def test_blocker_message_cc_to_lead(tmp_path):
    bus = MessageBus(tmp_path / "ws")
    bus.send("alice", "bob", "stuck on X", msg_type="blocker")
    assert bus.peek_inbox("lead")


def test_plain_chitchat_does_not_cc_lead(tmp_path):
    """Generic 'message' between teammates stays private."""
    bus = MessageBus(tmp_path / "ws")
    bus.send("alice", "bob", "what do you think?", msg_type="message")
    assert bus.peek_inbox("lead") == []


def test_cc_does_not_duplicate_when_recipient_is_lead(tmp_path):
    """If lead is already the direct recipient, don't double-write."""
    bus = MessageBus(tmp_path / "ws")
    bus.send("alice", "lead", "done", msg_type="result")
    assert len(bus.peek_inbox("lead")) == 1


def test_cc_fires_lead_hook_once(tmp_path):
    """Lead hook fires exactly once per send, even on CC path, and
    the payload is always lead-addressed (CC copy when CC fired,
    original when not). Downstream consumers rely on this stable
    shape to attribute messages correctly."""
    fired: list[dict] = []
    bus = MessageBus(tmp_path / "ws")
    bus.set_lead_hook(lambda m: fired.append(m))
    bus.send("alice", "bob", "done", msg_type="result")
    assert len(fired) == 1
    # Payload shape contract: always addressed to lead.
    assert fired[0]["to"] == "lead"
    assert fired[0]["content"] == "done"
    assert fired[0]["metadata"]["cc"] is True
    assert fired[0]["metadata"]["original_to"] == "bob"


def test_lead_as_sender_does_not_self_cc(tmp_path):
    """Lead sending a result-class message to a teammate must not
    generate a CC receipt back into lead's own mailbox — that would
    echo every outgoing milestone into the inbox."""
    bus = MessageBus(tmp_path / "ws")
    bus.send("lead", "bob", "ok done", msg_type="result")
    assert bus.peek_inbox("lead") == [], (
        "lead-as-sender must not self-CC; lead's mailbox should be "
        f"empty, got {bus.peek_inbox('lead')}"
    )


def test_cc_message_carries_metadata_for_origin(tmp_path):
    """CC copy must mark itself as a CC so the lead-side consumer
    knows the original recipient. This drives correct attribution
    in the UI (e.g. 'Alice reported a milestone to Bob')."""
    bus = MessageBus(tmp_path / "ws")
    bus.send("alice", "bob", "done", msg_type="result")
    lead_msgs = bus.peek_inbox("lead")
    assert lead_msgs
    cc = lead_msgs[0]
    assert cc["metadata"].get("cc") is True
    assert cc["metadata"].get("original_to") == "bob"


def test_cc_persists_to_history(tmp_path):
    """CC copy must land in lead's history log so /agents inbox lead
    can review milestones after the live mailbox is drained."""
    bus = MessageBus(tmp_path / "ws")
    bus.send("alice", "bob", "done", msg_type="result")
    # read_inbox drains the live cache; history should still have it.
    bus.read_inbox("lead")
    history = bus.history("lead")
    assert any(h["content"] == "done" for h in history)
