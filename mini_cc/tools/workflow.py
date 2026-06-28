"""Workflow tools — create / inspect / step through workflows.

These give the agent an imperative handle on a workflow in addition
to the static ``run_workflow`` API. Tools are best for one-off
"start a workflow and report progress" patterns; for repeatable
procedures, prefer defining the workflow as data and running it
through the runner directly.

State model: one *active* workflow per ToolContext (stored on the
project). The agent creates one, steps through it, and reads its
state as it goes. Concurrent workflows aren't supported — keep it
simple.

B11: per-wf reentrant lock guards workflow_run_all so two parallel
calls against the same wf_id serialize rather than corrupting the
state bag mid-write.
"""
from __future__ import annotations

import json
import threading
from typing import Any

from .base import FunctionTool, ToolContext
from ..workflow import (Workflow, WorkflowStep, run_workflow,
                        workflow_from_dict, workflow_from_markdown)


# B11: per-wf-id reentrant locks. Reentrant so a workflow can re-enter
# its own dispatcher (e.g. a step that triggers another workflow tool)
# without deadlocking.
_WF_LOCKS_GUARD = threading.Lock()
_WF_LOCKS: dict[str, threading.RLock] = {}


def _wf_lock(wf_id: str) -> threading.RLock:
    with _WF_LOCKS_GUARD:
        lk = _WF_LOCKS.get(wf_id)
        if lk is None:
            lk = threading.RLock()
            _WF_LOCKS[wf_id] = lk
        return lk


# ── workflow_create ──────────────────────────────────────────────────────

def _workflow_create(ctx: ToolContext, args: dict) -> str:
    """Create a new workflow from a JSON/dict or markdown definition."""
    project = ctx.project_ref
    if project is None:
        return "Error: project_ref not available on this context"

    wf_dict = args.get("workflow")
    md = args.get("markdown")
    name = args.get("name")

    if wf_dict and isinstance(wf_dict, dict):
        if name:
            wf_dict = dict(wf_dict)
            wf_dict.setdefault("name", name)
        wf = workflow_from_dict(wf_dict)
    elif md and isinstance(md, str):
        wf = workflow_from_markdown(md)
        if name:
            wf.name = name
    else:
        return ("Error: provide `workflow` (dict shape) or `markdown` "
                "(## sections become steps)")

    _set_active_workflow(ctx, wf)
    return _summarize(wf, header="✅ workflow created")


# ── workflow_add_step ────────────────────────────────────────────────────

def _workflow_add_step(ctx: ToolContext, args: dict) -> str:
    """Add a step to the active workflow at runtime (the dynamic path)."""
    wf = _get_active_workflow(ctx)
    if wf is None:
        return "Error: no active workflow. Call workflow_create first."
    step_id = (args.get("id") or "").strip()
    if not step_id:
        return "Error: id is required"
    prompt = args.get("prompt")
    if not prompt:
        return "Error: prompt is required"
    step = WorkflowStep(
        id=step_id,
        prompt=prompt,
        condition=args.get("condition"),
        parallel_with=args.get("parallel_with"),
        on_failure=args.get("on_failure", "skip"),
        max_retries=int(args.get("max_retries", 0)),
    )
    wf.add_step(step)
    return f"✅ added step `{step_id}` ({len(wf.steps)} total)"


# ── workflow_run_step ────────────────────────────────────────────────────

def _workflow_run_step(ctx: ToolContext, args: dict) -> str:
    """Run a single named step from the active workflow.

    Returns the step result. Conditions are evaluated: a false
    condition yields a 'skipped' message rather than dispatching.
    """
    wf = _get_active_workflow(ctx)
    if wf is None:
        return "Error: no active workflow."
    step_id = (args.get("id") or "").strip()
    if not step_id:
        return "Error: id is required"
    step = next((s for s in wf.steps if s.id == step_id), None)
    if step is None:
        return f"Error: unknown step `{step_id}`"

    # Substitute and evaluate condition.
    from ..workflow import _eval_condition, _substitute_prompt
    scope = dict(wf.state)
    scope.update(wf.results)
    if step.condition and not _eval_condition(step.condition, scope):
        return f"[step `{step_id}` skipped — condition `{step.condition}` is false]"
    prompt = _substitute_prompt(step.prompt, scope)

    # Dispatch via the active session's AgentLoop if available; else
    # surface the would-be prompt so the caller can dispatch manually.
    dispatcher = _build_dispatcher(ctx)
    if dispatcher is None:
        return (f"[step `{step_id}` ready to run] "
                f"dispatch_fn unavailable — prompt:\n\n{prompt}")
    try:
        result = dispatcher(prompt)
    except Exception as e:
        return f"Error running step `{step_id}`: {type(e).__name__}: {e}"
    wf.results[step_id] = result
    _persist_active(ctx, wf)
    return f"[step `{step_id}` completed]\n\n{result}"


# ── workflow_run_all ─────────────────────────────────────────────────────

def _workflow_run_all(ctx: ToolContext, args: dict) -> str:
    """Run every pending step in the active workflow.

    Walks the step list from the beginning; for each step that has
    not already been completed, evaluates its condition, dispatches
    it through the active session's AgentLoop, and records the result.
    Aborts the run on the first step whose ``on_failure`` is ``abort``
    and which fails; otherwise failures are reported inline but the
    walk continues.
    """
    wf = _get_active_workflow(ctx)
    if wf is None:
        return "Error: no active workflow."
    dispatcher = _build_dispatcher(ctx)
    if dispatcher is None:
        return ("Error: no dispatcher wired on this context. "
                "workflow_dispatch is attached by AgentLoop._make_ctx; "
                "this tool only works inside a running loop.")
    from ..workflow import run_workflow

    # B11: serialize concurrent run_all against the same wf_id. Without
    # this, two parallel calls would race on wf.state and step results,
    # potentially corrupting the persisted JSON.
    lock = _wf_lock(getattr(wf, "id", "_global"))
    if not lock.acquire(blocking=False):
        return (f"Error: workflow `{wf.id}` is already running in "
                "another call. Wait for it to finish before retrying.")
    try:
        events = list(run_workflow(wf, dispatcher,
                                   initial_state=wf.state))
    finally:
        lock.release()

    summary_lines: list[str] = []
    aborted = False
    for ev in events:
        et = ev["type"]
        if et == "step_started":
            summary_lines.append(f"▶ {ev['step_id']}")
        elif et == "step_skipped":
            summary_lines.append(f"⏭ {ev['step_id']} — skipped "
                                 f"({ev.get('reason', '?')})")
        elif et == "step_completed":
            result_text = (ev.get("result") or "").strip()
            first_line = result_text.splitlines()[0] if result_text else ""
            preview = first_line[:80]
            summary_lines.append(f"✅ {ev['step_id']} — {preview}")
        elif et == "step_failed":
            summary_lines.append(f"❌ {ev['step_id']} — {ev.get('error', '?')}")
        elif et == "workflow_aborted":
            aborted = True
            summary_lines.append(f"⛔ aborted: {ev.get('reason', '?')}")
        elif et == "workflow_completed":
            summary_lines.append("🏁 workflow completed")

    header = ("workflow run_all — aborted" if aborted
              else "workflow run_all — done")
    return f"**{header}**\n\n" + "\n".join(summary_lines)


# ── workflow_status ──────────────────────────────────────────────────────

def _workflow_status(ctx: ToolContext, args: dict) -> str:
    """Dump the active workflow's state."""
    wf = _get_active_workflow(ctx)
    if wf is None:
        return "_no active workflow in this project_"
    return _summarize(wf, header="📋 active workflow")


# ── workflow_set_state ───────────────────────────────────────────────────

def _workflow_set_state(ctx: ToolContext, args: dict) -> str:
    """Merge a dict into the workflow's state (for placeholder substitution)."""
    wf = _get_active_workflow(ctx)
    if wf is None:
        return "Error: no active workflow."
    merge = args.get("state") or {}
    if not isinstance(merge, dict):
        return "Error: state must be a dict"
    wf.state.update(merge)
    keys = ", ".join(sorted(merge.keys())) or "(empty)"
    return f"✅ state updated: {keys}"


# ── helpers ──────────────────────────────────────────────────────────────

def _get_active_workflow(ctx: ToolContext) -> Workflow | None:
    project = ctx.project_ref
    if project is None:
        return None
    return getattr(project, "active_workflow", None)


def _set_active_workflow(ctx: ToolContext, wf: Workflow) -> None:
    project = ctx.project_ref
    if project is None:
        return
    # Use object.__setattr__ if the project is frozen; else plain setattr.
    try:
        setattr(project, "active_workflow", wf)
    except Exception:
        object.__setattr__(project, "active_workflow", wf)
    _persist_active(ctx, wf)


def _persist_active(ctx: ToolContext, wf: Workflow | None) -> None:
    """Mirror the active workflow into storage so a server restart can
    restore it. Best-effort — silently skipped if storage doesn't
    implement the workflow methods (older Storage impls, in-memory test
    stubs)."""
    if wf is None:
        return
    storage = getattr(ctx, "storage", None) or getattr(ctx.project_ref, "storage", None)
    if storage is None or not hasattr(storage, "save_workflow"):
        return
    try:
        storage.save_workflow(ctx.project_id, wf.to_dict())
    except Exception:
        pass


def _summarize(wf: Workflow, *, header: str) -> str:
    lines = [f"**{header}**", "",
             f"- id: `{wf.id}`",
             f"- name: **{wf.name}**",
             f"- status: `{wf.status}`",
             f"- steps: {len(wf.steps)}",
             f"- completed: {len(wf.results)}"]
    if wf.description:
        lines.append(f"- description: {wf.description}")
    if wf.steps:
        lines.append("")
        lines.append("**Steps:**")
        for s in wf.steps:
            tag = "✅" if s.id in wf.results else "◻️"
            cond = f" _if `{s.condition}`_" if s.condition else ""
            par = f" _parallel with `{s.parallel_with}`_" if s.parallel_with else ""
            lines.append(f"- {tag} `{s.id}`{cond}{par}")
    return "\n".join(lines)


def _build_dispatcher(ctx: ToolContext):
    """Return a function(prompt) -> str that runs the prompt in-session.

    Falls back to None when no loop is reachable — callers handle it.
    """
    project = ctx.project_ref
    if project is None:
        return None
    # The session_manager keeps warm sessions; the project doesn't
    # hold a back-reference. We expose the dispatcher via a callable
    # attached at AgentLoop._make_ctx time when available.
    fn = getattr(ctx, "workflow_dispatch", None)
    if callable(fn):
        return fn
    return None


WORKFLOW_CREATE_TOOL = FunctionTool(
    name="workflow_create",
    description=(
        "Create a dynamic workflow from a dict or markdown definition. "
        "Workflows chain prompts together, with optional conditions and "
        "parallel steps. Once created, use workflow_add_step to extend "
        "it at runtime, workflow_run_step to execute one step, and "
        "workflow_status to inspect state."),
    input_schema={
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Workflow name."},
            "workflow": {
                "type": "object",
                "description": "Workflow dict with `steps: [{id, prompt, ...}]`.",
            },
            "markdown": {
                "type": "string",
                "description": "Markdown with ## sections, one per step.",
            },
        },
    },
    fn=_workflow_create,
)


WORKFLOW_ADD_STEP_TOOL = FunctionTool(
    name="workflow_add_step",
    description="Add a step to the active workflow at runtime.",
    input_schema={
        "type": "object",
        "properties": {
            "id": {"type": "string"},
            "prompt": {"type": "string"},
            "condition": {
                "type": "string",
                "description": "Python expression; step runs when truthy.",
            },
            "parallel_with": {"type": "string"},
            "on_failure": {
                "type": "string", "enum": ["skip", "abort", "retry"],
            },
            "max_retries": {"type": "integer"},
        },
        "required": ["id", "prompt"],
    },
    fn=_workflow_add_step,
)


WORKFLOW_RUN_STEP_TOOL = FunctionTool(
    name="workflow_run_step",
    description="Run a single step from the active workflow by id.",
    input_schema={
        "type": "object",
        "properties": {"id": {"type": "string"}},
        "required": ["id"],
    },
    fn=_workflow_run_step,
)


WORKFLOW_STATUS_TOOL = FunctionTool(
    name="workflow_status",
    description="Show the active workflow's steps, state, and completed results.",
    input_schema={"type": "object", "properties": {}},
    fn=_workflow_status,
)


WORKFLOW_RUN_ALL_TOOL = FunctionTool(
    name="workflow_run_all",
    description=(
        "Run every pending step in the active workflow, end-to-end. "
        "Each step is dispatched to the active session's AgentLoop "
        "via the wired workflow_dispatch. Returns a digest of "
        "started/completed/skipped/failed steps. Use this for "
        "fire-and-forget execution; use workflow_run_step for "
        "interactive step-by-step control."),
    input_schema={"type": "object", "properties": {}},
    fn=_workflow_run_all,
)


WORKFLOW_SET_STATE_TOOL = FunctionTool(
    name="workflow_set_state",
    description="Merge a dict into the workflow's state for {placeholder} substitution.",
    input_schema={
        "type": "object",
        "properties": {"state": {"type": "object"}},
        "required": ["state"],
    },
    fn=_workflow_set_state,
)


ALL = [
    WORKFLOW_CREATE_TOOL,
    WORKFLOW_ADD_STEP_TOOL,
    WORKFLOW_RUN_STEP_TOOL,
    WORKFLOW_RUN_ALL_TOOL,
    WORKFLOW_STATUS_TOOL,
    WORKFLOW_SET_STATE_TOOL,
]
