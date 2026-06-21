# Phase C: interactive permission prompts

## Context

Today `make_permission_hook` is a sync `PreToolUse` hook that returns
`None` (allow) or `str` (deny). In a backend / SDK context there's no
way to ask a human mid-turn — destructive commands just get denied
outright, which is safe but limiting. Phase C adds an interactive
path: certain tools, when invoked, prompt the client over SSE; the
client POSTs back a decision; the loop resumes.

Goals:

1. Per-tool opt-in: a project-level config file lists tools that
   require prompts (bash, fs_write, fs_edit).
2. Out-of-band decision: client gets a `permission_request` SSE event,
   responds via a separate `POST /permissions/{req_id}/decide`.
3. Hard timeout (default 300s): if no decision arrives, loop resumes
   with `[permission timed out]` as the tool_result.
4. Recoverable: `GET /permissions` lists pending requests so a client
   that missed the SSE event can resync.
5. Honors session stop / client disconnect — no thread hangs.

## Approach (locked via brainstorm)

- **Trigger policy**: per-tool opt-in via `<workspace>/.mini_cc/permissions.toml`.
- **Wait mechanism**: yield `permission_request` event, then block
  inside the loop on `threading.Event.wait(timeout)` polled at 1s
  intervals so `_stop` can interrupt.
- **Timeout**: hard 300s default, configurable in TOML.
- **Pending visibility**: `GET /sessions/{sid}/permissions` lists
  pending requests.
- **Wiring**: per-project config file. Missing file → no interceptor
  → today's behavior (no regression).

## File-by-file

### `mini_cc/core/permissions.py` (new, ~120 lines)

```python
@dataclass
class PermissionRequest:
    request_id: str
    session_id: str
    tool_name: str
    tool_input: dict
    created_at: str
    _decided: threading.Event = field(default_factory=threading.Event)
    decision: str | None = None    # "allow" | "deny"
    deny_message: str | None = None

class PermissionInterceptor:
    def __init__(self, timeout_seconds: int = 300): ...
    def create(self, session_id, tool_name, tool_input) -> PermissionRequest: ...
    def wait(self, request_id, stop_event: threading.Event | None = None)
        -> PermissionRequest | None: ...
    def decide(self, request_id, decision: str, message: str = "") -> bool: ...
    def list_pending(self, session_id) -> list[PermissionRequest]: ...
    def cancel(self, request_id) -> None: ...
```

`wait()` loops `Event.wait(timeout=1.0)` and checks `stop_event.is_set()`
so session stop unblocks the loop within ~1s.

### `mini_cc/projects/permissions_config.py` (new, ~50 lines)

```python
@dataclass
class PermissionsConfig:
    prompt_tools: set[str]
    timeout_seconds: int = 300

def load_permissions_config(workspace: Path) -> PermissionsConfig | None:
    """Read <workspace>/.mini_cc/permissions.toml. None if missing."""
```

Uses Python 3.11's `tomllib`. Malformed file → log warning, return
None (best-effort). Unknown tool names silently ignored.

### `mini_cc/core/loop.py` (modify)

- `ProjectRef` gains `permissions: PermissionInterceptor | None` and
  `prompt_tools: set[str]` fields (default empty).
- `AgentLoop.__init__` reads them from ProjectRef.
- In `_execute_tool_calls`, **before** the existing PreToolUse hook:

```python
if (self.project.permissions is not None
        and name in self.project.prompt_tools):
    req = self.project.permissions.create(
        self.session_id, name, tool_input)
    yield {"type": "permission_request",
           "request_id": req.request_id,
           "tool_name": name, "tool_input": tool_input,
           "id": tool_use_id}
    self._emit({"type": "permission_request", **...})
    decided = self.project.permissions.wait(req.request_id,
                                            stop_event=self._stop)
    if decided is None or decided.decision != "allow":
        output = (decided.deny_message if decided
                  else "[permission timed out]")
        yield {"type": "tool_result", "tool_use_id": tool_use_id,
               "content": output}
        self._emit({"type": "tool_result", ...})
        continue   # skip the actual tool execution
    # else: fall through to normal tool.handle(ctx, tool_input)
```

### `mini_cc/projects/manager.py` (modify)

- `_assemble`: call `load_permissions_config(ws)`. If non-None,
  construct `PermissionInterceptor(timeout_seconds=cfg.timeout_seconds)`,
  store on Project as `permissions` + `prompt_tools` fields.
- Project / ProjectRef pass these through `as_ref()`.

### `mini_cc/server/schemas.py` (modify)

Add `PermissionRequestOut` and `DecidePermissionRequest` pydantic
models.

### `mini_cc/server/routes/permissions.py` (new)

| Method | Path | Behavior |
| --- | --- | --- |
| `GET` | `/tenants/{tid}/projects/{pid}/sessions/{sid}/permissions` | list pending |
| `POST` | `/tenants/{tid}/projects/{pid}/sessions/{sid}/permissions/{req_id}/decide` | set decision |

Router wired into `app.py`. Routes pull `sm._ensure_warm(pid, sid).loop`
and read `_permissions`. If None → GET returns `[]`, POST returns 404.

### `tests/test_phaseC_interceptor.py` (new)

PermissionInterceptor unit tests: create/wait/decide/cancel/timeout.

### `tests/test_phaseC_loop.py` (new)

End-to-end AgentLoop test with a stub client that emits a bash
tool_use. Verifies: permission_request yielded, decide(allow) → tool
runs, decide(deny) → tool_result with message, timeout → tool_result
"[permission timed out]", session.stop() during wait → no hang.

### `tests/test_phaseC_config.py` (new)

TOML loader: missing/valid/malformed/unknown-tools.

### `tests/test_phaseC_http.py` (new)

TestClient flow with a workspace that has permissions.toml.

### `mini_cc/README.md` + `.zh.md` (modify)

Remove "Interactive permission prompts" from Out-of-scope. Add new
endpoints. New "Interactive permissions" section: TOML format, event
flow diagram, timeout, what clients need to implement.

## Verification

```bash
python -m pytest tests/test_phaseC_*.py -v
python -m pytest tests/ -q   # expect ~280 passing
```

Manual:

```bash
# Create project + add permissions.toml in its workspace
echo 'prompt_tools = ["bash"]' > <data_dir>/projects/p1/.mini_cc/permissions.toml

# Start server, create session, send "run ls"
# SSE stream emits: text + tool_use + permission_request
# In another shell:
curl -X POST .../permissions/<req_id>/decide \
     -H "Authorization: Bearer mck_..." \
     -d '{"decision":"allow"}'
# Original SSE stream resumes with tool_result + done.
```

## Out of scope for Phase C

- "Always allow" allowlist (Phase C only does prompt list).
- Per-tool-input rules (e.g. "prompt only if command contains `rm`").
  Today: prompt on every invocation of an opt-in tool.
- Persistent decisions ("don't ask again for this command").
- Multi-approver workflows.
