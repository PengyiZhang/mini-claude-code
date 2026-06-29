[ < [09](09-auth.md) ] [ [11](11-mcp-plugins.md) > ] · [中文版本](../zh/10-workflow-v2.md)

# 10 — Workflow V2

> The legacy `Workflow` stuffs "what to do" (template) and "what happened"
> (state + results) into one mutable object. V2 splits it three ways:
> `WorkflowDefinition` (versioned template), `WorkflowRun` (one execution),
> `StepRun` (one step record). Add `def_snapshot` pinning, `{step_id}`
> placeholder substitution, and five step types, and workflows can now
> pause/resume on external events like Airflow while staying reproducible.

---

## Problem & motivation

s20's `tools/workflow.py` is a fat dataclass where `prompt + state + results`
live together. In a multi-tenant backend three problems blow up immediately:

1. **Template and state are tangled**. You want to tweak a step's prompt —
   you end up mutating `state`. You want to rerun a failed step — `results`
   has been clobbered. Template and execution history should never share
   mutability.
2. **No "human approval" or "wait for external callback" semantics**. 90% of
   real production workflows need to pause mid-run — wait for a review, wait
   for a GitHub webhook, wait for a user to reply to an email. The legacy
   class could only run end-to-end; human-in-the-loop meant hacking a state
   machine at the application layer.
3. **Editing the definition mutates in-flight runs**. The `def_version`
   label existed but execution read the live def, so a mid-run PUT suddenly
   added or removed a step from a running run.

V2 (`mini_cc/workflow_v2.py`) ships in W1–W6 slices: Definition/Run
separation (W1), checkpoint human gate (W2), webhook_wait gate (W3),
email_wait gate (W4), validate deterministic check (W5), UI drive endpoint
+ `{step_id}` placeholder (W6), plus `def_snapshot` freezing the def at run
creation.

---

## Design & principles

### 1. The three-layer data model

```
WorkflowDefinition  (versioned template; PUT bumps version)
   │  def_id, version, steps: [StepDef...], triggers, state_schema
   │
   │  start_run() — copies current def into def_snapshot, builds WorkflowRun
   ▼
WorkflowRun  (one execution)
   │  run_id, def_id, def_version, status, current_step_idx
   │  state, step_runs: [StepRun...], def_snapshot  ←─ frozen def
   │
   │  each step completion appends a StepRun
   ▼
StepRun  (one step record, append-only)
   step_id, status, started_at, completed_at, output, error
```

`WorkflowDefinition` only changes via CRUD and `PUT` bumps `version`
(`workflow_v2.py:245`). `WorkflowRun` is built by `start_run` from a def
and accumulates `StepRun` records. All three are persisted independently
with symmetric `to_dict/from_dict`.

### 2. def_snapshot: editing the def mid-run can't mutate execution

`start_run` copies the current def into `run.def_snapshot`
(`workflow_v2.py:175`, `:269`). `drive_run` and all three `resolve_*`
methods read it through `_resolve_run_def`, never the live def:

```python
def _resolve_run_def(self, project_id, run):
    if run.def_snapshot:
        return WorkflowDefinition.from_dict(run.def_snapshot)
    return self.get_definition(project_id, run.def_id)   # legacy fallback
```

(`workflow_v2.py:280`). Without this, a mid-run PUT would change what the
next iteration of `drive_run` walks — `def_version` would be a lie.

### 3. drive_run: the state machine over 5 step types

`drive_run(dispatch_fn, max_steps=1000)` (`workflow_v2.py:315`) advances
from `current_step_idx`, with per-type behavior:

| Step type      | Behavior                                                                 |
|----------------|--------------------------------------------------------------------------|
| `action`       | Calls `dispatch_fn(prompt, run)` (through AgentLoop); stores the text reply in `run.state[step_id]` |
| `validate`     | No LLM; runs `_run_validate` against state (safe eval or JSON schema)    |
| `checkpoint`   | Flips `status="paused"`, `StepRun.status="paused"`, returns              |
| `webhook_wait` | Same as checkpoint; waits for `resolve_webhook_wait` to feed a payload   |
| `email_wait`   | Same as checkpoint; waits for `resolve_email_wait` to feed an email      |

Drive loop skeleton:

```python
for idx in range(run.current_step_idx, min(len(d.steps), max_steps)):
    step = d.steps[idx]
    if step.type in ("checkpoint", "webhook_wait", "email_wait"):
        run.status = "paused"; run.current_step_idx = idx
        sr.status = "paused"; ...; return run      # parking
    if step.type == "validate":
        valid, detail = self._run_validate(step, run.state); ...
    # else action
    scope = {**run.state, "step_id": step.id}       # NEW: {step_id} injected
    prompt = self._substitute(step.prompt, scope)
    result = dispatch_fn(prompt, run)
    run.state[step.id] = result
```

(`workflow_v2.py:341–412`)

### 4. {step_id} placeholder substitution (NEW)

Each action step's `prompt` supports `{name}` placeholders, resolved from
`run.state`. The new reserved key `step_id` (`workflow_v2.py:394`) lets a
template say "you are now on step `{step_id}`, continue from `step_xxx`'s
result". The substitution lives in `_substitute` (`workflow_v2.py:575`):

```python
@staticmethod
def _substitute(prompt, scope):
    def repl(m):
        v = scope.get(m.group(1))
        return str(v) if v is not None else m.group(0)   # leave as-is if missing
    return re.sub(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}", repl, prompt)
```

Unknown keys are left as literal `{xxx}` rather than crashing — so JSON
braces in a prompt aren't accidentally eaten.

### 5. validate's safe eval

`_run_validate` (`workflow_v2.py:520`) supports two check shapes:

- String expression: `{"check": "len(items) <= 10 and count > 0"}`, run
  through `eval` but with `__builtins__` pinned to `_VALIDATE_SAFE_BUILTINS`
  (a whitelist of len/str/int/min/max/sorted/isinstance etc., see
  `workflow_v2.py:510`).
- JSON schema: `{"check": {"schema": {"type": "object", "required": ["x"]}}}`
  goes through `_validate_schema` for a minimal type + required
  implementation (full validation needs the `jsonschema` package).

Gotcha worth flagging: **bool is not int**. `_validate_schema` explicitly
rejects `expected_type == "integer" and isinstance(value, bool)`
(`workflow_v2.py:562`), otherwise `True` would pass `isinstance(True, int)`
and be accepted as a valid integer.

### 6. The three gate resolvers

After a parking step, an external event triggers one of these resolvers to
advance the run:

```
checkpoint   ─ resolve_gate(decision="approve"|"reject", approver, feedback)
webhook_wait ─ resolve_webhook_wait(payload)              # implicit approve
email_wait   ─ resolve_email_wait(email_dict)             # implicit approve
```

All three share the same skeleton: validate step type + current
`sr.status == "paused"` → write gate_output to `run.state[step_id]` →
`current_step_idx += 1` → if past the end `status="completed"`, otherwise
stay `paused` until the next `drive_run`.

**Approver authorization** (`workflow_v2.py:464`): when the step configures
`config.approvers=["alice","bob"]`, the caller's `approver` must be in the
list; an empty/missing list means "anyone with `workflow:approve` scope"
(enforced by the HTTP layer).

### 7. webhook_wait's shared secret

External systems (GitHub/Stripe) can't carry a tenant bearer, so
`POST /runs/{rid}/webhook/{sid}` accepts either auth path
(`server/routes/workflow_v2.py:401`):

- standard tenant bearer + scope (in-house callers / tests), OR
- a `?webhook_id=...` query param matching `step.config.webhook_id` —
  this is the shared secret that replaces the tenant boundary.

`resolve_webhook_wait` also supports `config.event_filter`
(`workflow_v2.py:619`): one webhook URL can serve multiple wait steps,
dispatched on `payload["event"]`.

---

## Operation & configuration

### HTTP routes (`mini_cc/server/routes/workflow_v2.py`)

| Method | Path | Scope | Purpose |
|--------|------|-------|---------|
| GET    | `/tenants/{tid}/projects/{pid}/workflow-definitions` | `projects:read` | List defs |
| POST   | `/tenants/{tid}/projects/{pid}/workflow-definitions` | `projects:write` | Create def |
| GET    | `.../workflow-definitions/{def_id}` | `projects:read` | Get def |
| PUT    | `.../workflow-definitions/{def_id}` | `projects:write` | Update def (version+1) |
| DELETE | `.../workflow-definitions/{def_id}` | `projects:write` | Delete def |
| POST   | `.../workflow-definitions/{def_id}/runs` | `projects:write` | Start a run |
| GET    | `/tenants/{tid}/projects/{pid}/workflow-runs` | `projects:read` | List runs (optional `?def_id=`) |
| GET    | `.../workflow-runs/{run_id}` | `projects:read` | Get run |
| DELETE | `.../workflow-runs/{run_id}` | `projects:write` | Cancel run |
| POST   | `.../workflow-runs/{run_id}/drive` | `projects:write` | Drive synchronously, body `{"session_id":"..."}` |
| POST   | `.../workflow-runs/{run_id}/steps/{sid}/resolve` | `projects:write` | Resolve a checkpoint gate |
| POST   | `.../workflow-runs/{run_id}/webhook/{sid}` | (webhook_id OR bearer) | Resolve webhook_wait |
| POST   | `.../workflow-runs/{run_id}/email/{sid}` | (no auth; filters gate) | Resolve email_wait |

### Per-step config knobs (in the `config` dict)

| Step type | config fields |
|-----------|---------------|
| `checkpoint` | `approvers: [str]` (optional) |
| `webhook_wait` | `webhook_id: str` (shared secret), `event_filter: str` (optional) |
| `email_wait` | `from_filter: str`, `subject_filter: str` (substring, case-insensitive) |
| `validate` | `check: str \| {"schema": {...}}` |

### Run status values

`pending | running | paused | completed | failed | cancelled`
(`workflow_v2.py:166`)

---

## Verification steps

Backend on `:8002`. First create a tenant / project / API key with at least
`projects:write` scope.

```bash
BASE=http://localhost:8002
TENANT=t_demo; PROJ=p_demo
AUTH="Authorization: Bearer <YOUR_KEY>"

# 1) Create a definition with a checkpoint
curl -s -X POST "$BASE/tenants/$TENANT/projects/$PROJ/workflow-definitions" \
  -H "$AUTH" -H "Content-Type: application/json" -d '{
    "name": "approve-demo",
    "steps": [
      {"id":"draft","type":"action","prompt":"Draft a one-paragraph release note for {feature}."},
      {"id":"review","type":"checkpoint","config":{"approvers":["alice"]}},
      {"id":"publish","type":"action","prompt":"Publish the approved note. Context: {review}"}
    ],
    "state_schema": {}
  }' | jq '.def_id, .version'

# 2) Start a run
DEF=wfdef_xxx     # id from previous step
curl -s -X POST "$BASE/tenants/$TENANT/projects/$PROJ/workflow-definitions/$DEF/runs" \
  -H "$AUTH" -H "Content-Type: application/json" \
  -d '{"initial_state":{"feature":"streaming search"},"trigger":{"type":"manual"}}' \
  | jq '.run_id, .status, .current_step_idx'

# 3) Drive (the run parks at the checkpoint)
RUN=wfrun_xxx; SESS=sess_xxx
curl -s -X POST "$BASE/tenants/$TENANT/projects/$PROJ/workflow-runs/$RUN/drive" \
  -H "$AUTH" -H "Content-Type: application/json" \
  -d "{\"session_id\":\"$SESS\"}" | jq '.status, .current_step_idx, .step_runs[1].status'
# Expect: status="paused", step_runs[1].status="paused"

# 4) Resolve the gate (must be alice)
curl -s -X POST "$BASE/tenants/$TENANT/projects/$PROJ/workflow-runs/$RUN/steps/review/resolve" \
  -H "$AUTH" -H "Content-Type: application/json" \
  -d '{"decision":"approve","approver":"alice","feedback":"lgtm"}' \
  | jq '.status, .current_step_idx'

# 5) Drive once more to run publish
curl -s -X POST "$BASE/tenants/$TENANT/projects/$PROJ/workflow-runs/$RUN/drive" \
  -H "$AUTH" -H "Content-Type: application/json" -d "{\"session_id\":\"$SESS\"}" \
  | jq '.status, .state.review, .state.publish'
```

UI polling: the frontend shows Approve/Reject buttons when status is
`paused`; after calling `resolve` it calls `drive` once more so the run
advances to the next park point or `completed`.

---

## Common pitfalls / debugging

1. **`def_version` changed but behavior didn't?** You edited the def but
   didn't start a new run — the old run executes its `def_snapshot`. To see
   what a run actually executes, read `run.def_snapshot.steps`, not the
   live def.
2. **`{step_id}` didn't substitute?** Placeholders must be valid Python
   identifiers `[a-zA-Z_][a-zA-Z0-9_]*` and the key must be in `run.state`
   or the reserved `step_id`. JSON `{}` in a prompt is left as-is (unknown
   keys are preserved literally).
3. **validate always passes?** A missing `check` is treated as a no-op pass
   (`workflow_v2.py:532`). Remember to write `check` in step.config.
4. **webhook_wait returns 401 `invalid or missing webhook_id`?** The step
   configured `config.webhook_id` but the caller didn't pass the query
   param, or the value doesn't match (`server/routes/workflow_v2.py:428`).
   It's the shared secret replacing the tenant bearer.
5. **email_wait says "email does not match step filters"?** `from_filter` /
   `subject_filter` are substring + case-insensitive matches
   (`workflow_v2.py:692`); only empty filters let any email through.
6. **Approver rejected with "not in step's approvers list"?** The step set
   `approvers=["alice"]` but you passed `approver="bob"`
   (`workflow_v2.py:465`). An empty list means "any workflow:approve scope
   holder".

---

## Further reading

- Source: `mini_cc/workflow_v2.py` (full 733 lines),
  `mini_cc/server/routes/workflow_v2.py` (HTTP surface)
- Legacy workflow (for contrast): `mini_cc/workflow.py` +
  `mini_cc/tools/workflow.py`
- 3-tier plugins / `.mini_cc/` layout: next chapter
  [11 — MCP Clients & 3-tier Plugins](11-mcp-plugins.md)
- The companion `/workflow` slash command (operates on the legacy Workflow,
  not V2): `mini_cc/commands/registry.py:873`
- e2e coverage matrix: `mini_cc/tests/e2e/NOTES.md` (W1/W4/W6 specs)
