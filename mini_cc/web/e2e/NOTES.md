# mini_cc/web/e2e — coverage matrix & notes

## Coverage matrix

| Spec file                                  | Test                                                               | Phase       | Status |
|--------------------------------------------|--------------------------------------------------------------------|-------------|--------|
| `workflow_v2.spec.ts`                      | authors a definition and starts a run                              | W6          | ✓      |
| `workflow_v2.spec.ts`                      | renders empty state when no definitions exist                      | W6          | ✓      |
| `workflow_v2_advanced.spec.ts`             | pauses at checkpoint and Approve advances the run                  | W2 + W6     | ✓      |
| `workflow_v2_resolvers.spec.ts`            | W2: checkpoint Reject fails the run (API)                          | W2          | ✓      |
| `workflow_v2_resolvers.spec.ts`            | W3: webhook_wait resolves via inbound webhook POST                 | W3          | ✓      |
| `workflow_v2_resolvers.spec.ts`            | W5: validate step fails the run on falsy check                     | W5          | ✓      |
| `workflow_v2_resolvers.spec.ts`            | W5: validate step passes on truthy check                           | W5          | ✓      |
| `workflow_v2_def_crud.spec.ts`             | edit a definition and bump its version                             | W6          | ✓      |
| `workflow_v2_def_crud.spec.ts`             | delete a definition removes it from the left pane                  | W6          | ✓      |
| `workflow_v2_email.spec.ts`                | W4: matching email advances the step                               | W4          | ✓      |
| `workflow_v2_email.spec.ts`                | W4: non-matching email rejected (400), step stays paused           | W4          | ✓      |
| `workflow_v2_email.spec.ts`                | W4: resolving a non-paused step is rejected (400)                  | W4          | ✓      |
| `workflow_v2_w1_api.spec.ts`               | W1: create → get → list → update (version bump) → delete           | W1          | ✓      |
| `workflow_v2_w1_api.spec.ts`               | W1: list with no defs returns empty array                          | W1          | ✓      |
| `workflow_v2_w1_api.spec.ts`               | W1: update on a missing def returns 404                            | W1          | ✓      |
| `workflow_v2_polling.spec.ts`              | W6: UI picks up external webhook resolution without refresh        | W6          | ✓      |
| `round2_hardening.spec.ts`                 | a workflow def in one tenant is invisible to another               | R2 / P0-1   | ✓*     |
| `round2_hardening.spec.ts`                 | a single authenticated GET is allowed (smoke)                      | R2 / B6     | ✓      |
| `round2_sse.spec.ts`                       | /send accepts Last-Event-Id header and returns text/event-stream   | R2 / B8     | ✓      |

✓* requires `E2E_ALT_TENANT_KEY` / `E2E_ALT_TENANT_ID` / `E2E_ALT_TENANT_PID` env vars; skipped otherwise.

Total: **19 passing**, 0 skipped (when fully provisioned), 0 flaky.

## Setup

```
# 1) Backend on :8002 using MINI_CC_DATA_DIR=mini_cc_data_e2e
python -m mini_cc.server serve

# 2) Frontend dev server on :5174 (5173 is occupied by another app on this machine)
cd mini_cc/web && npx vite --port 5174 --strictPort=false

# 3) Provision e2e tenant + key + project (only the project step — the
#    e2e tenant was provisioned earlier with the demo key):
curl -sS -X POST http://127.0.0.1:8002/tenants/e2e/projects \
  -H "Authorization: Bearer mck_REDACTED" \
  -H "Content-Type: application/json" \
  -d '{"project_id":"e2e_proj","display_name":"e2e"}'

# 4) (Optional) Cross-tenant boundary test needs a second tenant/key:
python -m mini_cc.server keygen e2e_other --scopes "projects:read" --scopes "projects:write"
curl -sS -X POST http://127.0.0.1:8002/tenants/e2e_other/projects \
  -H "Authorization: Bearer mck_..." \
  -d '{"project_id":"e2e_other_proj","display_name":"xtenant"}'

# 5) Run
cd mini_cc/web
E2E_API_KEY=mck_REDACTED \
E2E_ALT_TENANT_KEY=mck_... \
E2E_ALT_TENANT_ID=e2e_other \
E2E_ALT_TENANT_PID=e2e_other_proj \
npx playwright test --project=chromium
```

## Bugs surfaced and fixed during this round

| Bug                                                                                              | Fix commit    |
|--------------------------------------------------------------------------------------------------|---------------|
| `playwright.config.ts` baseURL pointed at port 5173, which is occupied by another app on this machine → all e2e timed out | `7aa4fab`     |
| `helpers.ts` used `getByLabel` for sign-in, but `Login.tsx`'s `<Field>` wrapper puts the label text in a `<div>` inside `<label>`, which Playwright doesn't match reliably | `7aa4fab`     |
| `workflow_v2.spec.ts` used `getByRole('button', { name: /new definition/ })` but the button has only `title="new definition"` (visible text is `＋`) — switched to `getByTitle` | `7aa4fab`     |
| **UX gap**: after creating a def, the middle pane said "select a run from the left" with no way to create the first run (the `↻ new run` button only renders inside `RunView`, which only mounts once a run exists). Added a `▶ start new run` button to the empty-state placeholder in `WorkflowV2.tsx`. | `7aa4fab`     |
| **UX gap**: `DefinitionEditor` had no delete button — the only way to remove a def was a direct `DELETE` API call. Added a 🗑 delete button to the editor footer (edit-mode only, with `window.confirm`). | `15685e6`     |

## Leftover issues / not covered

- **Drive endpoint with action steps**: action steps dispatch through the AgentLoop, which needs a working LLM. The dev backend has `llm_configured: true` but real LLM calls are flaky in CI. Specs are designed so action-step dispatch is never the *first* thing exercised (gates/webhook/validate come first), and where an action step is the second step (e.g., W2 approve test) only gate resolution is asserted, not action completion.
- **W4 IMAP poll loop**: the inbound email resolver is exercised via direct HTTP POST (matching/rejecting/state errors). The actual `EmailService.poll_once` IMAP path needs a real IMAP server; covered in `tests/test_round2_w4.py`.
- **Round 2 Batch 3–5 backend** (atomic writes, SSE queue bound, loop run guard, etc.): all covered by `tests/test_round2_b*.py`. No e2e because they need fault injection (kill -9, network partition, etc.).
- **Pre-existing `setup.spec.ts` flakiness**: chat test asserts `body.length > 50`, but the LLM sometimes returns <50 chars. Unrelated to this round; left alone.
- **SSE mid-stream reconnect (B4 Last-Event-Id replay)**: the endpoint accepts the header (smoke in `round2_sse.spec.ts`), but verifying replay-from-disk requires a live LLM stream + mid-stream disconnect. The storage layer (`append_session_event` / `read_session_events_since`) is covered in `tests/test_round2_b4.py`.
- **Client-side SSE auto-reconnect**: `lib/sse.ts` does NOT auto-reconnect on drop — it just calls `onError`. The server supports replay, but the client never resumes. Real gap; candidate for a future UX round.

## Cron / scheduling

A recurring cron job (`ae0f3bd3`) fires daily at **06:10 local** to continue this work in YOLO no-ask mode. Auto-expires after 7 days. Cancel with `CronDelete ae0f3bd3`.
