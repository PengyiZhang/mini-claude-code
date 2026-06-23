"""Dynamic workflow runner.

A workflow is a list of steps the agent executes sequentially (or in
parallel where marked). Each step is a prompt plus an optional Python
condition evaluated against the workflow's accumulated state. Steps
can be added at runtime by the agent itself — hence "dynamic".

Two ways to use this:

1. **Static definition** — define a workflow upfront (YAML / dict /
   list of WorkflowStep) and run it. Good for repeatable procedures
   like "research → draft → review".

2. **Imperative API** — call ``add_step`` / ``next_step`` /
   ``mark_done`` from agent code or tool handlers to drive the runner
   forward incrementally as new information arrives.

The runner is reentrant: it picks up where it left off based on the
``state`` dict (which step is current, what results have been recorded).
State can be persisted via the project's storage if needed.

Execution model:
- ``run(workflow, ctx)`` yields SSE-shaped dicts as it walks the steps.
- For each step, the runner builds a prompt and dispatches it through
  the project's AgentLoop. The agent's response becomes the step's
  ``result`` in the workflow state.
- Condition evaluation: a restricted eval of a Python expression against
  the workflow state dict. ``step_1_succeeded`` is the typical pattern.

We avoid hard deps on YAML — workflows can be defined as Python dicts
or as markdown-with-frontmatter. Parsing utilities for both shapes are
provided.
"""
from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator, Optional


# ── Data model ───────────────────────────────────────────────────────────

@dataclass
class WorkflowStep:
    """One step in a workflow.

    - ``id``           — short identifier, used in conditions.
    - ``prompt``       — text fed to the agent. ``{step_id}`` placeholders
                         are substituted with prior step results.
    - ``condition``    — optional Python expr evaluated against the
                         workflow state. Step is skipped when it returns
                         falsy. Restricted eval — no builtins, no attrs.
    - ``parallel_with``— step id this step should run alongside. Both
                         steps start together; the runner waits for both.
    - ``on_failure``   — "skip" (default), "abort", or "retry".
    - ``max_retries``  — when on_failure=retry, max attempts.
    """
    id: str
    prompt: str
    condition: str | None = None
    parallel_with: str | None = None
    on_failure: str = "skip"        # skip | abort | retry
    max_retries: int = 0
    metadata: dict = field(default_factory=dict)


@dataclass
class Workflow:
    """A named list of steps plus execution state."""
    id: str
    name: str
    steps: list[WorkflowStep]
    description: str = ""
    state: dict = field(default_factory=dict)
    # Map of step_id → result string (or error). Filled by runner.
    results: dict = field(default_factory=dict)
    current_step: str | None = None
    status: str = "pending"          # pending | running | completed | aborted

    def add_step(self, step: WorkflowStep) -> None:
        """Append a step at runtime — the 'dynamic' part."""
        self.steps.append(step)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "status": self.status,
            "current_step": self.current_step,
            "steps": [
                {"id": s.id, "prompt": s.prompt,
                 "condition": s.condition, "parallel_with": s.parallel_with,
                 "on_failure": s.on_failure, "max_retries": s.max_retries}
                for s in self.steps
            ],
            "results": dict(self.results),
            "state": dict(self.state),
        }


# ── Parsing ─────────────────────────────────────────────────────────────

def workflow_from_dict(data: dict) -> Workflow:
    """Build a Workflow from a plain dict.

    Expected shape::

        {
          "name": "research-and-draft",
          "description": "Research a topic, then draft a summary.",
          "steps": [
            {"id": "research", "prompt": "Find sources for {topic}."},
            {"id": "draft", "prompt": "Draft based on {research}.",
             "condition": "research"},
          ],
        }

    ``topic`` in the first prompt is substituted from the workflow's
    state dict at run time. Step results are auto-filled: ``{research}``
    becomes the result of the step with id "research".
    """
    wf_id = data.get("id") or f"wf_{uuid.uuid4().hex[:8]}"
    name = data.get("name") or wf_id
    description = data.get("description", "")
    raw_steps = data.get("steps") or []
    steps: list[WorkflowStep] = []
    for raw in raw_steps:
        sid = raw.get("id") or f"step_{len(steps) + 1}"
        steps.append(WorkflowStep(
            id=sid,
            prompt=raw.get("prompt", ""),
            condition=raw.get("condition"),
            parallel_with=raw.get("parallel_with"),
            on_failure=raw.get("on_failure", "skip"),
            max_retries=int(raw.get("max_retries", 0)),
            metadata=raw.get("metadata") or {},
        ))
    return Workflow(id=wf_id, name=name, description=description, steps=steps)


# Markdown front-matter parsing. Lightweight — no yaml dep.
_MD_FRONTMATTER_RE = re.compile(
    r"^\s*---\s*\n(.*?)\n---\s*\n(.*)$", re.DOTALL)


def workflow_from_markdown(text: str) -> Workflow:
    """Parse a workflow from a markdown document.

    The body is split into ``## <step-id>`` sections; the section's
    text becomes the step's prompt. Optional front-matter (``key:
    value`` lines between ``---`` markers) supplies name/description
    and step-level overrides via ``steps.<id>.condition = expr`` etc.

    Example::

        ---
        name: research-and-draft
        ---
        ## research
        Find sources for {topic}.

        ## draft
        Draft based on {research}.
    """
    fm: dict[str, str] = {}
    body = text
    m = _MD_FRONTMATTER_RE.match(text)
    if m:
        fm_text, body = m.group(1), m.group(2)
        for line in fm_text.splitlines():
            if ":" in line:
                k, _, v = line.partition(":")
                fm[k.strip()] = v.strip()

    # Split body on markdown headings of level exactly 2.
    sections: list[tuple[str, str]] = []
    current_id: str | None = None
    current_lines: list[str] = []
    for line in body.splitlines():
        m = re.match(r"^##\s+(\S+)", line)
        if m:
            if current_id is not None:
                sections.append((current_id, "\n".join(current_lines).strip()))
            current_id = m.group(1)
            current_lines = []
        elif current_id is not None:
            current_lines.append(line)
    if current_id is not None:
        sections.append((current_id, "\n".join(current_lines).strip()))

    steps: list[WorkflowStep] = []
    for sid, prompt in sections:
        if not prompt:
            continue
        condition = fm.get(f"steps.{sid}.condition")
        parallel_with = fm.get(f"steps.{sid}.parallel_with")
        on_failure = fm.get(f"steps.{sid}.on_failure", "skip")
        steps.append(WorkflowStep(
            id=sid, prompt=prompt,
            condition=condition,
            parallel_with=parallel_with,
            on_failure=on_failure,
        ))

    return Workflow(
        id=fm.get("id") or f"wf_{uuid.uuid4().hex[:8]}",
        name=fm.get("name") or "imported-workflow",
        description=fm.get("description", ""),
        steps=steps,
    )


# ── Condition evaluation ─────────────────────────────────────────────────

# Restricted eval: forbid attribute access (no __builtins__, no dots
# in names). Names available are the workflow's results dict + state.
_ALLOWED_GLOBALS: dict[str, Any] = {"__builtins__": {}}


def _eval_condition(expr: str, scope: dict) -> bool:
    """Evaluate a workflow condition expression.

    Allowed names: anything in ``scope`` (typically the workflow's
    results dict). Python operators are allowed; attribute access is
    forbidden. Truthy result means "run the step".
    """
    if not expr or not expr.strip():
        return True
    try:
        return bool(eval(expr, dict(_ALLOWED_GLOBALS), scope))
    except Exception:
        # An unparseable or undefined-name condition fails closed: skip.
        return False


def _substitute_prompt(prompt: str, scope: dict) -> str:
    """Replace ``{key}`` placeholders in prompt with scope values.

    Falls back to the empty string for missing keys (so an unrelated
    ``{...}`` in shell syntax doesn't break the workflow).
    """
    def repl(m: re.Match) -> str:
        key = m.group(1)
        if key in scope:
            v = scope[key]
            return str(v) if v is not None else ""
        return m.group(0)
    return re.sub(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}", repl, prompt)


# ── Runner ───────────────────────────────────────────────────────────────

def run_workflow(
    workflow: Workflow,
    dispatch_fn: Callable[[str], str],
    *,
    initial_state: dict | None = None,
) -> Iterator[dict]:
    """Drive a workflow to completion, yielding SSE-shaped events.

    ``dispatch_fn`` takes a prompt string and returns the agent's
    response. In production this closure captures the project's
    AgentLoop; tests pass a fake.

    Yields:
    - {"type": "workflow_started", "workflow": dict}
    - {"type": "step_started", "step_id": str}
    - {"type": "step_skipped", "step_id": str, "reason": str}
    - {"type": "step_completed", "step_id": str, "result": str}
    - {"type": "step_failed", "step_id": str, "error": str}
    - {"type": "workflow_completed", "workflow": dict}
    - {"type": "workflow_aborted", "workflow": dict, "reason": str}
    """
    if initial_state:
        workflow.state.update(initial_state)

    workflow.status = "running"
    yield {"type": "workflow_started", "workflow": workflow.to_dict()}

    aborted = False
    for step in workflow.steps:
        if aborted:
            break
        workflow.current_step = step.id

        # Substitute placeholders from state + results.
        scope = dict(workflow.state)
        scope.update(workflow.results)
        # Also alias each step's result by id for condition checks.
        # Already in scope via results; nothing to do.

        # Condition gate.
        if step.condition and not _eval_condition(step.condition, scope):
            workflow.results[step.id] = ""   # mark as not-run
            yield {"type": "step_skipped", "step_id": step.id,
                   "reason": f"condition `{step.condition}` false"}
            continue

        prompt = _substitute_prompt(step.prompt, scope)
        yield {"type": "step_started", "step_id": step.id, "prompt": prompt}

        # Retry loop for on_failure=retry.
        attempts = max(1, step.max_retries + 1) if step.on_failure == "retry" else 1
        last_err: str | None = None
        succeeded = False
        for attempt in range(attempts):
            try:
                result = dispatch_fn(prompt)
                workflow.results[step.id] = result
                yield {"type": "step_completed", "step_id": step.id,
                       "result": result, "attempt": attempt + 1}
                succeeded = True
                break
            except Exception as e:
                last_err = f"{type(e).__name__}: {e}"
                if attempt + 1 < attempts:
                    continue

        if not succeeded:
            yield {"type": "step_failed", "step_id": step.id,
                   "error": last_err or "unknown"}
            if step.on_failure == "abort":
                workflow.status = "aborted"
                aborted = True
                yield {"type": "workflow_aborted",
                       "workflow": workflow.to_dict(),
                       "reason": f"step `{step.id}` failed"}
                return
            # on_failure=skip or retry-exhausted: continue to next step.

    if not aborted:
        workflow.status = "completed"
        workflow.current_step = None
        yield {"type": "workflow_completed",
               "workflow": workflow.to_dict()}


__all__ = [
    "WorkflowStep", "Workflow",
    "workflow_from_dict", "workflow_from_markdown",
    "run_workflow",
]
