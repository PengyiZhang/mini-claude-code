"""Teams subsystem — per-project message bus, protocol tracker, spawner.

Ports s20 lines 491-872:
- MessageBus: per-project JSONL mailboxes (not module-global)
- ProtocolTracker: pending plan-approval / shutdown requests keyed by id
- TeammateSpawner: per-project registry; runs each teammate in a daemon
  thread with a sub-AgentLoop. Implements the plan-approval gate at the
  turn boundary and the autonomous idle poll with auto-claim.

Behaviour deltas vs s20 (documented simplifications):
- Plan-approval gate is enforced between turns, not mid-turn. The
  submit_plan tool tells the model to end its turn; the spawner blocks
  the next turn until review_plan arrives.
- Auto-cwd into a claimed task's worktree is implemented via
  AgentLoop.set_worktree: after a successful claim, the spawner swaps
  the teammate's sandbox root to the worktree path. The teammate gets
  its own sandbox (built in projects.manager._build_teammate_loop) so
  the swap doesn't affect other sessions.

Implementation lives in the sibling modules (M3-2 split): bus.py,
protocol.py, spawner.py, convention.py. This file only re-exports the
public surface so existing `from ..teams import X` paths keep working.
"""
from .bus import MessageBus, _format_inbox_as_dialogue
from .protocol import ProtocolState, ProtocolTracker
from .spawner import TeammateInfo, TeammateSpawner
from .convention import convention_prompt

__all__ = ["MessageBus", "ProtocolState", "ProtocolTracker",
           "TeammateInfo", "TeammateSpawner", "convention_prompt",
           "_format_inbox_as_dialogue"]
