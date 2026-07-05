# Teammates: stop signal, encoding, smart prompt (2026-07-05)

Three teammate-subsystem fixes shipped together. Root causes were found
by reading the actual code; the prior `sse.py`-blames-encoding hypothesis
was wrong (the SSE wire is JSON-parsed by the browser, so `\u` escapes
round-trip — and the symptom was on refresh, not live stream).

## 1. Park-after-result (stop signal)

**Symptom:** after a teammate finished its task it kept making LLM calls
instead of waiting for instructions.

**Root cause:** the runner's inner `while True` already re-polls on idle
timeout *without* re-running the LLM (the "25+ identical writes" bug was
fixed long ago). The CURRENT path was different: `_idle_poll` wakes the
teammate on **any** inbox message. So `result` → LeadWatcher nudges lead
→ lead acks → ack lands in mailbox → teammate wakes for a full LLM turn
→ emits another result → repeat. Ack ping-pong.

**Fix:** `msg_type="result"` now means "my whole mission is done."
- `MessageBus.send` records a per-agent flag (`_result_pending`) when
  `msg_type == "result"`; `take_result_flag(agent)` reads+clears it
  per-agent (concurrent teammates can't race-clear each other).
- `TeammateInfo.parked: bool`. The runner drains the flag after each
  turn and sets `info.parked = True` if the teammate sent a result.
- `_idle_poll` branches on `parked`: it peeks (no drain) and only wakes
  for `shutdown_request`, a lead `mention` (new task), or an unclaimed
  task. Chatter is buffered, not acted on. Wake clears `parked`.

**Why mention-only:** the convention now states the lead @mentions a
parked teammate to assign new work. Plain messages to a parked teammate
are acks/chatter and stay buffered. This is what breaks the ping-pong.

## 2. Garbled text after refresh (encoding)

**Symptom:** teammate-* sessions (and any peer viewing them) showed
literal `你…` escape sequences in chat bubbles after a page refresh.

**Root cause:** `_format_inbox_as_dialogue` appended a `<!-- raw: … -->`
fallback line built with `json.dumps(inbox)` — default `ensure_ascii=True`
— so Chinese content became literal backslash-u escapes. That string is
persisted as the teammate's `user_input` in the transcript, so the
escapes survived to the rendered bubble. Lead-side injection
(`loop._inject_teammate_replies`, `watcher._format_nudge`) was already
`ensure_ascii=False` / human-readable — main session was clean.

**Fix:** `ensure_ascii=False` at `teams/__init__.py` `_format_inbox_as_dialogue`
(critical), plus the mailbox `_append_disk` writes and `server/sse.py`
`_format_event` (defensive, same root-cause class). Regression test
`test_inbox_dialogue_preserves_unicode` pins Chinese round-tripping.

## 3. Phase-aware convention prompt

**Symptom:** the old convention forced `submit_plan` before *every*
non-trivial action — rigid, not smart. The user wants teammates to
deliberate, submit one consolidated plan, get one approval, then execute
fluidly (the crosstalk example: 3 teammates discuss → one submits →
approved → perform without per-line approval).

**Fix:** rewrote `_CONVENTION_PROMPT` to be phase-based and autonomy-
oriented: DELIBERATE → plan approval *when warranted* (one coordinator
files a consolidated plan for multi-agent work; no re-approval per
step) → REPORT BY STAGE (`milestone` = intermediate, `result` = whole
mission done → parks you). Trimmed the `<identity>` block to name/role
(the convention owns protocol). Kept the keywords the test suite pins
(`submit_plan`, `send_message`, `lead`, `shutdown`, `inbox`).

The three fixes interlock: the park semantic only works because the
prompt now tells the model `result` = mission-complete (not per-step),
and the crosstalk flow works because teammates exchange `message`
(not `result`) during deliberation/performance, so they never park
mid-collaboration.

## Verification

- `test_teammate_real_llm.py` (real glm model via .env proxy): passes —
  end-to-end exercise of new prompt + park logic with a live LLM.
- New: `test_inbox_dialogue_preserves_unicode`,
  `test_teammate_parks_after_result_and_ignores_chatter`.
- Full teams/lead/watcher/bus suite: 110 passed.

## Known pre-existing flakes (not addressed here)

- `test_mailbox_production.py::test_cross_process_locking_is_attempted`
  fails when `portalocker` isn't installed (env-only).
- `test_inbox_disposition.py::test_dispositions_for_agent_returns_all`
  flakes ~50% on Windows: two rapid `bus.send` calls can share a
  `time.time()` stamp, colliding the disposition key. Separate from
  these three fixes.
