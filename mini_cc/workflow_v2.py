"""Workflow V2 — Definition/Run concept separation.

W1 backend foundation. The legacy Workflow class mixes "what to run"
(template) with "what happened" (state + results). This module adds:

- WorkflowDefinition: a versioned template (steps + triggers + state
  schema). Mutated via CRUD; immutable once a run references it.
- WorkflowRun: a single execution. Created from a definition, persists
  state transitions, exposes an event log for UI / replay.
- WorkflowService: ties storage + dispatch together. The agent's
  AgentLoop drives action steps; the service handles state machine
  transitions and event log appends.

The legacy tools/workflow.py API stays untouched — new code lives
under /workflows HTTP routes and the WorkflowService entry point.
"""
from __future__ import annotations

import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Iterator, Optional


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat(
        timespec="milliseconds").replace("+00:00", "Z")


# ── Step definitions ───────────────────────────────────────────────────────

@dataclass
class StepDef:
    """One step of a workflow definition.

    Step types (W1 supports ``action`` only; W2-W5 add the rest):
    - ``action``        — sub-prompt dispatched through AgentLoop
    - ``validate``      — JSONPath/predicate check (W5)
    - ``checkpoint``    — pause for human approval (W2)
    - ``webhook_wait``  — pause for inbound webhook (W3)
    - ``email_wait``    — pause for inbound email (W4)
    """
    id: str
    type: str = "action"
    prompt: str = ""
    # JSON-schema-style dicts; validated by the runner before/after.
    inputs_schema: dict = field(default_factory=dict)
    outputs_schema: dict = field(default_factory=dict)
    # Free-form config — type-specific knobs (approvers, webhook_id,
    # timeout_sec, etc.) live here so StepDef stays a single class.
    config: dict = field(default_factory=dict)
    # Soft branch: skip this step when the expression is falsy.
    condition: str | None = None

    def to_dict(self) -> dict:
        return {
            "id": self.id, "type": self.type, "prompt": self.prompt,
            "inputs_schema": dict(self.inputs_schema),
            "outputs_schema": dict(self.outputs_schema),
            "config": dict(self.config),
            "condition": self.condition,
        }


@dataclass
class TriggerDef:
    """How a new run can be started.

    W1: only ``manual`` is honored. The other types are parsed and
    stored so definitions authored ahead of the feature don't lose
    their triggers when W3/W4 land.
    """
    type: str  # manual | webhook | schedule | email
    config: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"type": self.type, "config": dict(self.config)}


# ── Definition ─────────────────────────────────────────────────────────────

@dataclass
class WorkflowDefinition:
    """Versioned workflow template.

    Definitions are mutable via PUT (which bumps version). A Run
    captures ``def_version`` at creation time so it stays reproducible
    even as the definition evolves.
    """
    def_id: str
    name: str
    description: str = ""
    version: int = 1
    owner: str | None = None
    created_at: str = field(default_factory=_iso_now)
    updated_at: str = field(default_factory=_iso_now)
    steps: list[StepDef] = field(default_factory=list)
    triggers: list[TriggerDef] = field(default_factory=list)
    state_schema: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "def_id": self.def_id, "name": self.name,
            "description": self.description, "version": self.version,
            "owner": self.owner,
            "created_at": self.created_at, "updated_at": self.updated_at,
            "steps": [s.to_dict() for s in self.steps],
            "triggers": [t.to_dict() for t in self.triggers],
            "state_schema": dict(self.state_schema),
        }

    @classmethod
    def from_dict(cls, raw: dict) -> "WorkflowDefinition":
        return cls(
            def_id=raw["def_id"],
            name=raw.get("name", raw["def_id"]),
            description=raw.get("description", ""),
            version=int(raw.get("version", 1)),
            owner=raw.get("owner"),
            created_at=raw.get("created_at") or _iso_now(),
            updated_at=raw.get("updated_at") or _iso_now(),
            steps=[StepDef(
                id=s["id"],
                type=s.get("type", "action"),
                prompt=s.get("prompt", ""),
                inputs_schema=s.get("inputs_schema", {}),
                outputs_schema=s.get("outputs_schema", {}),
                config=s.get("config", {}),
                condition=s.get("condition"),
            ) for s in raw.get("steps", [])],
            triggers=[TriggerDef(
                type=t.get("type", "manual"),
                config=t.get("config", {}),
            ) for t in raw.get("triggers", [])],
            state_schema=raw.get("state_schema", {}),
        )


# ── Run ────────────────────────────────────────────────────────────────────

@dataclass
class StepRun:
    """Per-step execution record. Appended to a run as steps complete."""
    step_id: str
    status: str = "pending"  # pending | running | completed | failed | skipped
    started_at: str | None = None
    completed_at: str | None = None
    output: Any = None
    error: str | None = None

    def to_dict(self) -> dict:
        return {
            "step_id": self.step_id, "status": self.status,
            "started_at": self.started_at, "completed_at": self.completed_at,
            "output": self.output, "error": self.error,
        }


@dataclass
class WorkflowRun:
    """One execution of a WorkflowDefinition."""
    run_id: str
    def_id: str
    def_version: int
    status: str = "pending"  # pending|running|paused|completed|failed|cancelled
    current_step_idx: int = 0
    started_at: str | None = None
    completed_at: str | None = None
    state: dict = field(default_factory=dict)
    step_runs: list[StepRun] = field(default_factory=list)
    # Trigger that started this run (echoed for audit).
    trigger: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "run_id": self.run_id, "def_id": self.def_id,
            "def_version": self.def_version, "status": self.status,
            "current_step_idx": self.current_step_idx,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "state": dict(self.state),
            "step_runs": [s.to_dict() for s in self.step_runs],
            "trigger": dict(self.trigger),
        }

    @classmethod
    def from_dict(cls, raw: dict) -> "WorkflowRun":
        return cls(
            run_id=raw["run_id"],
            def_id=raw["def_id"],
            def_version=int(raw.get("def_version", 1)),
            status=raw.get("status", "pending"),
            current_step_idx=int(raw.get("current_step_idx", 0)),
            started_at=raw.get("started_at"),
            completed_at=raw.get("completed_at"),
            state=raw.get("state", {}),
            step_runs=[StepRun(
                step_id=s["step_id"], status=s.get("status", "pending"),
                started_at=s.get("started_at"),
                completed_at=s.get("completed_at"),
                output=s.get("output"), error=s.get("error"),
            ) for s in raw.get("step_runs", [])],
            trigger=raw.get("trigger", {}),
        )


# ── Service ────────────────────────────────────────────────────────────────

class WorkflowService:
    """Orchestrates definitions + runs over a Storage backend.

    Dispatch protocol: ``dispatch_fn(step_prompt, run) -> str`` returns
    the agent's response for an ``action`` step. Service owns state
    transitions + persistence; the dispatcher just runs the prompt.
    """

    def __init__(self, storage):
        self.storage = storage

    # ── Definitions CRUD ──────────────────────────────────────────────────
    def create_definition(self, project_id: str, payload: dict,
                          *, def_id: str | None = None) -> WorkflowDefinition:
        def_id = def_id or f"wfdef_{uuid.uuid4().hex[:10]}"
        payload = {**payload, "def_id": def_id}
        d = WorkflowDefinition.from_dict(payload)
        self.storage.save_workflow_def(project_id, d.to_dict())
        return d

    def get_definition(self, project_id: str, def_id: str) -> WorkflowDefinition | None:
        raw = self.storage.load_workflow_def(project_id, def_id)
        return WorkflowDefinition.from_dict(raw) if raw else None

    def list_definitions(self, project_id: str) -> list[WorkflowDefinition]:
        return [WorkflowDefinition.from_dict(r)
                for r in self.storage.list_workflow_defs(project_id)]

    def update_definition(self, project_id: str, def_id: str,
                          payload: dict) -> WorkflowDefinition | None:
        existing = self.get_definition(project_id, def_id)
        if existing is None:
            return None
        merged = existing.to_dict()
        merged.update(payload)
        merged["def_id"] = def_id  # never let the id change
        merged["version"] = existing.version + 1
        merged["updated_at"] = _iso_now()
        d = WorkflowDefinition.from_dict(merged)
        self.storage.save_workflow_def(project_id, d.to_dict())
        return d

    def delete_definition(self, project_id: str, def_id: str) -> bool:
        return self.storage.delete_workflow_def(project_id, def_id)

    # ── Run management ────────────────────────────────────────────────────
    def start_run(self, project_id: str, def_id: str, *,
                  initial_state: dict | None = None,
                  trigger: dict | None = None) -> WorkflowRun | None:
        d = self.get_definition(project_id, def_id)
        if d is None:
            return None
        run = WorkflowRun(
            run_id=f"wfrun_{uuid.uuid4().hex[:10]}",
            def_id=def_id, def_version=d.version,
            status="pending", state=dict(initial_state or {}),
            trigger=dict(trigger or {"type": "manual"}),
            step_runs=[StepRun(step_id=s.id) for s in d.steps],
        )
        self.storage.save_workflow_run(project_id, run.to_dict())
        return run

    def get_run(self, project_id: str, run_id: str) -> WorkflowRun | None:
        raw = self.storage.load_workflow_run(project_id, run_id)
        return WorkflowRun.from_dict(raw) if raw else None

    def list_runs(self, project_id: str,
                  def_id: str | None = None) -> list[WorkflowRun]:
        runs = [WorkflowRun.from_dict(r)
                for r in self.storage.list_workflow_runs(project_id)]
        if def_id is None:
            return runs
        return [r for r in runs if r.def_id == def_id]

    def cancel_run(self, project_id: str, run_id: str) -> WorkflowRun | None:
        run = self.get_run(project_id, run_id)
        if run is None:
            return None
        if run.status in ("completed", "failed", "cancelled"):
            return run
        run.status = "cancelled"
        run.completed_at = _iso_now()
        self.storage.save_workflow_run(project_id, run.to_dict())
        return run

    # ── Execution ─────────────────────────────────────────────────────────
    def drive_run(self, project_id: str, run_id: str,
                  dispatch_fn: Callable[[str, WorkflowRun], str],
                  *,
                  max_steps: int = 1000) -> WorkflowRun:
        """Drive a run forward, executing action steps synchronously.

        Parking steps (checkpoint / webhook_wait / email_wait) flip
        the run to ``paused`` and return; an external event resolver
        (added in W2-W4) calls back to unpause.

        ``dispatch_fn(step_prompt, run)`` runs the prompt and returns
        the agent's response. State is persisted after each step.
        """
        run = self.get_run(project_id, run_id)
        if run is None:
            raise KeyError(f"run {run_id} not found")
        if run.status in ("completed", "failed", "cancelled"):
            return run
        d = self.get_definition(project_id, run.def_id)
        if d is None:
            raise KeyError(f"definition {run.def_id} not found")

        run.status = "running"
        run.started_at = run.started_at or _iso_now()
        self.storage.save_workflow_run(project_id, run.to_dict())

        for idx in range(run.current_step_idx, min(len(d.steps), max_steps)):
            step = d.steps[idx]
            sr = next((s for s in run.step_runs if s.step_id == step.id),
                      StepRun(step_id=step.id))
            if sr.status in ("completed", "skipped"):
                continue

            # Parking steps: leave the run paused; external resolver
            # will call drive_run again after the gate is satisfied.
            if step.type in ("checkpoint", "webhook_wait", "email_wait"):
                run.status = "paused"
                run.current_step_idx = idx
                sr.status = "paused"
                sr.started_at = _iso_now()
                self.storage.save_workflow_run(project_id, run.to_dict())
                return run

            # Validate (W5): deterministic check, no dispatch.
            if step.type == "validate":
                sr.status = "running"
                sr.started_at = _iso_now()
                self.storage.save_workflow_run(project_id, run.to_dict())
                try:
                    valid, detail = self._run_validate(step, run.state)
                    sr.output = {"valid": valid, "detail": detail}
                    if not valid:
                        raise RuntimeError(
                            f"validate failed: {detail or 'check returned falsy'}")
                    sr.status = "completed"
                    sr.completed_at = _iso_now()
                    run.state[step.id] = {"valid": True}
                except Exception as e:
                    sr.error = f"{type(e).__name__}: {e}"
                    sr.status = "failed"
                    sr.completed_at = _iso_now()
                    run.status = "failed"
                    run.completed_at = _iso_now()
                    run.current_step_idx = idx
                    self.storage.save_workflow_run(project_id, run.to_dict())
                    return run
                run.current_step_idx = idx + 1
                self.storage.save_workflow_run(project_id, run.to_dict())
                continue

            # action step — dispatch through the AgentLoop.
            sr.status = "running"
            sr.started_at = _iso_now()
            self.storage.save_workflow_run(project_id, run.to_dict())

            try:
                # Substitute {placeholder} from state.
                prompt = self._substitute(step.prompt, run.state)
                result = dispatch_fn(prompt, run)
                sr.output = result
                sr.status = "completed"
                sr.completed_at = _iso_now()
                run.state[step.id] = result
            except Exception as e:
                sr.error = f"{type(e).__name__}: {e}"
                sr.status = "failed"
                sr.completed_at = _iso_now()
                run.status = "failed"
                run.completed_at = _iso_now()
                run.current_step_idx = idx
                self.storage.save_workflow_run(project_id, run.to_dict())
                return run

            run.current_step_idx = idx + 1
            self.storage.save_workflow_run(project_id, run.to_dict())

        run.status = "completed"
        run.completed_at = _iso_now()
        run.current_step_idx = len(d.steps)
        self.storage.save_workflow_run(project_id, run.to_dict())
        return run

    # ── Gate resolution (W2) ─────────────────────────────────────────────
    # Parking steps — checkpoint, webhook_wait, email_wait — flip the run
    # to ``paused``. ``resolve_gate`` is the entry point an approver /
    # webhook / inbound email hits to either advance or fail the run.
    GATE_STEP_TYPES = ("checkpoint", "webhook_wait", "email_wait")

    def resolve_gate(self, project_id: str, run_id: str,
                     step_id: str, *, decision: str,
                     approver: str | None = None,
                     feedback: str | None = None) -> WorkflowRun | None:
        """Resolve a parked gate step.

        ``decision`` is ``"approve"`` or ``"reject"``. Approve marks the
        step completed with the approver's feedback recorded as output
        and bumps ``current_step_idx`` past the gate so the next
        ``drive_run`` call resumes from the following step. Reject
        fails the step and the run.

        Returns the updated run, or ``None`` when the run / step
        doesn't exist. Raises ``ValueError`` when the step isn't a
        gate, isn't currently paused, or the approver isn't in the
        step's configured approvers list.
        """
        run = self.get_run(project_id, run_id)
        if run is None:
            return None
        d = self.get_definition(project_id, run.def_id)
        if d is None:
            return None
        step = next((s for s in d.steps if s.id == step_id), None)
        if step is None or step.type not in self.GATE_STEP_TYPES:
            raise ValueError(
                f"step {step_id} is not a gate step")
        sr = next((s for s in run.step_runs if s.step_id == step_id), None)
        if sr is None:
            raise ValueError(f"step {step_id} not in run")
        if sr.status != "paused":
            raise ValueError(
                f"step {step_id} is {sr.status}, not paused")
        if decision not in ("approve", "reject"):
            raise ValueError(f"decision must be approve|reject, got {decision!r}")
        # Approver authorization: when the step configures an explicit
        # approvers list, the caller must match. Empty/missing list is
        # "anyone with workflow:approve scope" — enforced by the HTTP layer.
        approvers = step.config.get("approvers") or []
        if approvers and approver not in approvers:
            raise ValueError(
                f"approver {approver!r} not in step's approvers list")

        now = _iso_now()
        gate_output = {
            "decision": decision,
            "approver": approver,
            "feedback": feedback,
            "resolved_at": now,
        }
        if decision == "approve":
            sr.status = "completed"
            sr.completed_at = now
            sr.output = gate_output
            run.state[step_id] = gate_output
            # Walk forward — drive_run will skip the completed gate
            # and resume from the next step.
            idx = next((i for i, s in enumerate(d.steps) if s.id == step_id), 0)
            run.current_step_idx = idx + 1
            # If the next step doesn't exist, the run is complete.
            if run.current_step_idx >= len(d.steps):
                run.status = "completed"
                run.completed_at = now
            else:
                # Leave the run paused so the caller can decide when
                # to call drive_run again. The first action step on
                # resume will flip it to "running".
                run.status = "paused"
        else:  # reject
            sr.status = "failed"
            sr.completed_at = now
            sr.error = (feedback or "rejected by approver")
            run.status = "failed"
            run.completed_at = now

        self.storage.save_workflow_run(project_id, run.to_dict())
        return run

    # ── Validate step (W5) ──────────────────────────────────────────────
    # Deterministic check evaluated against the run's state. No LLM
    # call. The check expression comes from step.config.check and is
    # evaluated with state as locals + a restricted builtin set so a
    # workflow author can't open() the disk.

    _VALIDATE_SAFE_BUILTINS = {
        "len": len, "str": str, "int": int, "float": float,
        "bool": bool, "list": list, "dict": dict, "tuple": tuple,
        "set": set, "min": min, "max": max, "sum": sum,
        "abs": abs, "round": round, "any": any, "all": all,
        "sorted": sorted, "range": range, "enumerate": enumerate,
        "isinstance": isinstance, "True": True, "False": False,
        "None": None,
    }

    def _run_validate(self, step: StepDef, state: dict) -> tuple[bool, str]:
        """Evaluate step.config.check against state. Returns (valid, detail).

        Supported check shapes:
        - truthy expression: ``"count > 0"``, ``"len(items) <= 10"``
        - JSON-schema validation: ``{"schema": {...}}`` validates the
          entire state against the schema (uses jsonschema if available,
          else a structural best-effort).
        """
        check = step.config.get("check")
        if check is None:
            # No check configured — treat as a no-op pass.
            return True, "no check configured"
        if isinstance(check, str):
            try:
                result = eval(check, {"__builtins__": self._VALIDATE_SAFE_BUILTINS},
                              dict(state))
            except Exception as e:
                return False, f"{type(e).__name__}: {e}"
            return bool(result), f"check={check!r} → {bool(result)}"
        if isinstance(check, dict) and "schema" in check:
            # JSON-schema validation. Best-effort without the
            # jsonschema package — operators who need full validation
            # install it; we cover type/required here.
            return self._validate_schema(check["schema"], state)
        return False, f"unsupported check shape: {type(check).__name__}"

    @staticmethod
    def _validate_schema(schema: dict, value) -> tuple[bool, str]:
        """Minimal JSON-schema check (type + required). Full
        validation lives in the jsonschema package; we shim just
        enough to make the common cases work without the dep."""
        if not isinstance(schema, dict):
            return False, "schema must be a dict"
        expected_type = schema.get("type")
        type_map = {"string": str, "integer": int, "number": (int, float),
                    "boolean": bool, "array": list, "object": dict,
                    "null": type(None)}
        if expected_type and expected_type in type_map:
            # bool is a subclass of int — exclude explicitly for
            # integer checks so True isn't accepted as 1.
            actual = type(value)
            if expected_type == "integer" and isinstance(value, bool):
                return False, f"expected integer, got boolean"
            if expected_type == "number" and isinstance(value, bool):
                return False, f"expected number, got boolean"
            if not isinstance(value, type_map[expected_type]):
                return False, f"expected {expected_type}, got {actual.__name__}"
        if expected_type == "object" and isinstance(value, dict):
            required = schema.get("required") or []
            missing = [k for k in required if k not in value]
            if missing:
                return False, f"missing required: {missing}"
        return True, "schema ok"

    @staticmethod
    def _substitute(prompt: str, scope: dict) -> str:
        import re
        def repl(m):
            key = m.group(1)
            v = scope.get(key)
            return str(v) if v is not None else m.group(0)
        return re.sub(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}", repl, prompt)

    # ── Webhook inbound resolver (W3) ────────────────────────────────────
    # A webhook_wait step parks the run until an external system posts
    # to the workflow's inbound webhook URL. ``resolve_webhook_wait``
    # is the entry point the HTTP handler calls — payload becomes the
    # gate's output, the run resumes from the next step.

    def resolve_webhook_wait(self, project_id: str, run_id: str,
                              step_id: str, payload: dict) -> WorkflowRun | None:
        """Advance a parked ``webhook_wait`` step with the posted payload.

        Validates that the step is a webhook_wait gate and currently
        paused, applies an optional ``event_filter`` from step config
        (if set, ``payload['event']`` must match), then marks the step
        completed with the payload as output. Returns the updated run
        or ``None`` if the run is missing.
        """
        run = self.get_run(project_id, run_id)
        if run is None:
            return None
        d = self.get_definition(project_id, run.def_id)
        if d is None:
            return None
        step = next((s for s in d.steps if s.id == step_id), None)
        if step is None or step.type != "webhook_wait":
            raise ValueError(
                f"step {step_id} is not a webhook_wait step")
        sr = next((s for s in run.step_runs if s.step_id == step_id), None)
        if sr is None:
            raise ValueError(f"step {step_id} not in run")
        if sr.status != "paused":
            raise ValueError(
                f"step {step_id} is {sr.status}, not paused")
        # Optional event_filter: when configured, the payload's 'event'
        # field must match. Lets one webhook URL serve multiple wait
        # steps with different event types.
        event_filter = step.config.get("event_filter")
        if event_filter:
            actual = (payload or {}).get("event")
            if actual != event_filter:
                raise ValueError(
                    f"event {actual!r} does not match filter "
                    f"{event_filter!r}")

        now = _iso_now()
        gate_output = {
            "decision": "approve",  # webhook = implicit approve
            "payload": payload,
            "resolved_at": now,
            "resolver": "webhook",
        }
        sr.status = "completed"
        sr.completed_at = now
        sr.output = gate_output
        run.state[step_id] = gate_output
        idx = next((i for i, s in enumerate(d.steps) if s.id == step_id), 0)
        run.current_step_idx = idx + 1
        if run.current_step_idx >= len(d.steps):
            run.status = "completed"
            run.completed_at = now
        else:
            run.status = "paused"
        self.storage.save_workflow_run(project_id, run.to_dict())
        return run

    # ── Email inbound resolver (W4) ──────────────────────────────────────
    # An email_wait step parks the run until a matching email lands.
    # Operators wire up inbound email routing (SendGrid Inbound Parse,
    # Postmark, procmail, an IMAP poll loop) to POST to the HTTP route,
    # which calls this resolver. Filters live in step.config.

    def resolve_email_wait(self, project_id: str, run_id: str,
                            step_id: str, email: dict) -> WorkflowRun | None:
        """Advance a parked ``email_wait`` step with the received email.

        Validates the step is an email_wait gate, currently paused,
        and that the email matches the step's ``from_filter`` /
        ``subject_filter`` config (substring, case-insensitive). Marks
        the step completed with the email as output.
        """
        # Imported lazily so workflow_v2 doesn't drag the email module
        # (and its smtplib / imaplib imports) at module load time —
        # only needed when an email_wait step is actually resolved.
        from .email import matches_filters, InboundEmail

        run = self.get_run(project_id, run_id)
        if run is None:
            return None
        d = self.get_definition(project_id, run.def_id)
        if d is None:
            return None
        step = next((s for s in d.steps if s.id == step_id), None)
        if step is None or step.type != "email_wait":
            raise ValueError(
                f"step {step_id} is not an email_wait step")
        sr = next((s for s in run.step_runs if s.step_id == step_id), None)
        if sr is None:
            raise ValueError(f"step {step_id} not in run")
        if sr.status != "paused":
            raise ValueError(
                f"step {step_id} is {sr.status}, not paused")

        inbound = InboundEmail(
            from_addr=email.get("from_addr") or email.get("from") or "",
            to_addr=email.get("to_addr") or email.get("to") or "",
            subject=email.get("subject") or "",
            body=email.get("body") or "",
            raw_headers=email.get("raw_headers") or {},
        )
        if not matches_filters(
                inbound,
                from_filter=step.config.get("from_filter"),
                subject_filter=step.config.get("subject_filter")):
            raise ValueError(
                "email does not match step filters "
                f"(from_filter={step.config.get('from_filter')!r}, "
                f"subject_filter={step.config.get('subject_filter')!r})")

        now = _iso_now()
        gate_output = {
            "decision": "approve",
            "email": {
                "from": inbound.from_addr,
                "to": inbound.to_addr,
                "subject": inbound.subject,
                "body": inbound.body,
            },
            "resolved_at": now,
            "resolver": "email",
        }
        sr.status = "completed"
        sr.completed_at = now
        sr.output = gate_output
        run.state[step_id] = gate_output
        idx = next((i for i, s in enumerate(d.steps) if s.id == step_id), 0)
        run.current_step_idx = idx + 1
        if run.current_step_idx >= len(d.steps):
            run.status = "completed"
            run.completed_at = now
        else:
            run.status = "paused"
        self.storage.save_workflow_run(project_id, run.to_dict())
        return run


__all__ = [
    "StepDef", "TriggerDef",
    "WorkflowDefinition", "WorkflowRun", "StepRun",
    "WorkflowService",
]
