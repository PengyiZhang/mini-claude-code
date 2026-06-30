import { useState } from "react";
import type { TenantProfile, WorkflowV2Step, WorkflowV2StepType } from "../../lib/types";
import { ApiError, createWorkflowV2Def, deleteWorkflowV2Def, updateWorkflowV2Def } from "../../lib/api";

/**
 * Definition editor — modal form for authoring / editing a workflow
 * definition. Steps are added/removed/reordered inline; per-type
 * config fields surface as you change the step type.
 *
 * New definitions POST to /workflow-definitions; edits PUT to
 * /workflow-definitions/{def_id} (which bumps the version server-side).
 */
interface Props {
  profile: TenantProfile;
  pid: string;
  target:
    | { mode: "new" }
    | { mode: "edit"; def: EditorDef };
  onClose: () => void;
  onSaved: () => void;
}

type EditorDef = {
  def_id: string;
  name: string;
  description: string;
  version: number;
  owner: string | null;
  created_at: string;
  updated_at: string;
  steps: WorkflowV2Step[];
  triggers: { type: string; config: Record<string, unknown> }[];
  state_schema: Record<string, unknown>;
};

const STEP_TYPES: WorkflowV2StepType[] = [
  "action",
  "validate",
  "checkpoint",
  "webhook_wait",
  "email_wait",
  "branch",
  "loop",
];

const TYPE_HELP: Record<WorkflowV2StepType, string> = {
  action: "Sub-prompt dispatched through the AgentLoop.",
  validate: "Deterministic check; no LLM call. Fails the run on falsy result.",
  checkpoint: "Pause for human approval (approvers optional).",
  webhook_wait: "Pause for an inbound webhook (config.webhook_id optional shared secret).",
  email_wait: "Pause for an inbound email (from_filter / subject_filter optional).",
  branch: "Conditional jump. config.branches=[{when, next}]; first truthy wins. Optional config.default_next.",
  loop: "Iterate a body. config.body=[step_ids]; config.while=<expr>; config.max_iterations=N (default 100).",
};

export default function DefinitionEditor({ profile, pid, target, onClose, onSaved }: Props) {
  const existing = target.mode === "edit" ? target.def : null;
  const [name, setName] = useState(existing?.name ?? "");
  const [description, setDescription] = useState(existing?.description ?? "");
  const [steps, setSteps] = useState<WorkflowV2Step[]>(
    existing?.steps ?? [{ id: "step_1", type: "action", prompt: "" }],
  );
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  function addStep() {
    const n = steps.length + 1;
    setSteps([...steps, { id: `step_${n}`, type: "action", prompt: "" }]);
  }

  function removeStep(idx: number) {
    setSteps(steps.filter((_, i) => i !== idx));
  }

  function moveStep(idx: number, dir: -1 | 1) {
    const j = idx + dir;
    if (j < 0 || j >= steps.length) return;
    const next = [...steps];
    [next[idx], next[j]] = [next[j], next[idx]];
    setSteps(next);
  }

  function patchStep(idx: number, patch: Partial<WorkflowV2Step>) {
    setSteps(steps.map((s, i) => (i === idx ? { ...s, ...patch } : s)));
  }

  async function save() {
    if (!name.trim()) {
      setError("name is required");
      return;
    }
    if (steps.length === 0) {
      setError("at least one step is required");
      return;
    }
    // Validate unique step ids — the runner keys step outputs by id,
    // so duplicates would silently overwrite earlier results.
    const ids = new Set<string>();
    for (const s of steps) {
      if (!s.id.trim()) {
        setError("every step needs an id");
        return;
      }
      if (ids.has(s.id)) {
        setError(`duplicate step id: ${s.id}`);
        return;
      }
      ids.add(s.id);
    }

    setBusy(true);
    setError(null);
    try {
      const payload = {
        name: name.trim(),
        description: description.trim(),
        steps: steps.map((s) => ({
          id: s.id,
          type: s.type,
          prompt: s.prompt ?? "",
          config: s.config ?? {},
          condition: s.condition ?? null,
          inputs_schema: s.inputs_schema ?? {},
          outputs_schema: s.outputs_schema ?? {},
        })),
      };
      if (target.mode === "new") {
        await createWorkflowV2Def(profile, pid, payload);
      } else {
        await updateWorkflowV2Def(profile, pid, target.def.def_id, payload);
      }
      onSaved();
    } catch (e) {
      setError(e instanceof ApiError ? e.message : (e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  async function remove() {
    if (target.mode !== "edit") return;
    if (!confirm(`delete ${target.def.name} (v${target.def.version})? runs are preserved but can no longer be restarted.`)) return;
    setBusy(true);
    setError(null);
    try {
      await deleteWorkflowV2Def(profile, pid, target.def.def_id);
      onSaved();
    } catch (e) {
      setError(e instanceof ApiError ? e.message : (e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div
      className="fixed inset-0 z-50 flex justify-center items-center bg-black/40"
      onClick={onClose}
    >
      <div
        className="w-full max-w-3xl max-h-[90vh] bg-bg border border-border rounded-lg shadow-xl flex flex-col"
        onClick={(e) => e.stopPropagation()}
      >
        {/* Header */}
        <div className="flex items-center justify-between px-4 py-3 border-b border-border">
          <div className="text-sm font-medium text-ink">
            {target.mode === "new" ? "new workflow definition" : `edit · ${target.def.name} (v${target.def.version})`}
          </div>
          <button
            onClick={onClose}
            className="text-ink-dim hover:text-ink text-lg px-2"
          >
            ✕
          </button>
        </div>

        {/* Body */}
        <div className="flex-1 overflow-auto p-4 space-y-4">
          {error && (
            <div className="text-xs text-err bg-err/10 border border-err/40 rounded px-3 py-2">
              {error}
            </div>
          )}

          <div className="grid grid-cols-2 gap-3">
            <label className="block">
              <span className="text-xs text-ink-dim uppercase tracking-wide">name</span>
              <input
                type="text"
                value={name}
                onChange={(e) => setName(e.target.value)}
                placeholder="release-deploy"
                className="mt-1 w-full bg-bg-panel border border-border rounded px-3 py-1.5 text-sm outline-none focus:border-accent"
              />
            </label>
            <label className="block">
              <span className="text-xs text-ink-dim uppercase tracking-wide">description</span>
              <input
                type="text"
                value={description}
                onChange={(e) => setDescription(e.target.value)}
                placeholder="what does this workflow do?"
                className="mt-1 w-full bg-bg-panel border border-border rounded px-3 py-1.5 text-sm outline-none focus:border-accent"
              />
            </label>
          </div>

          {/* Steps */}
          <div className="space-y-3">
            <div className="flex items-center justify-between">
              <span className="text-xs text-ink-dim uppercase tracking-wide">
                steps ({steps.length})
              </span>
              <button
                onClick={addStep}
                className="text-xs px-2 py-1 rounded border border-border hover:border-accent text-ink-dim hover:text-ink"
              >
                ＋ add step
              </button>
            </div>

            {steps.map((s, i) => (
              <StepEditor
                key={i}
                step={s}
                index={i}
                total={steps.length}
                onChange={(patch) => patchStep(i, patch)}
                onRemove={() => removeStep(i)}
                onMove={(dir) => moveStep(i, dir)}
              />
            ))}
          </div>
        </div>

        {/* Footer */}
        <div className="border-t border-border px-4 py-3 flex justify-end gap-2">
          {target.mode === "edit" && (
            <button
              onClick={remove}
              disabled={busy}
              className="text-xs px-3 py-1.5 rounded border border-err/40 text-err hover:bg-err/10 disabled:opacity-50 mr-auto"
              title="delete this definition (runs are preserved)"
            >
              {busy ? "…" : "delete"}
            </button>
          )}
          <button
            onClick={onClose}
            className="text-xs px-3 py-1.5 rounded border border-border hover:border-ink-dim text-ink-dim"
          >
            cancel
          </button>
          <button
            onClick={save}
            disabled={busy}
            className="text-xs px-4 py-1.5 rounded bg-accent text-white hover:bg-accent-hover disabled:opacity-50"
          >
            {busy ? "saving…" : target.mode === "new" ? "create" : "save (bumps version)"}
          </button>
        </div>
      </div>
    </div>
  );
}

function StepEditor({
  step,
  index,
  total,
  onChange,
  onRemove,
  onMove,
}: {
  step: WorkflowV2Step;
  index: number;
  total: number;
  onChange: (patch: Partial<WorkflowV2Step>) => void;
  onRemove: () => void;
  onMove: (dir: -1 | 1) => void;
}) {
  const cfg = (step.config ?? {}) as Record<string, unknown>;
  function patchConfig(key: string, value: unknown) {
    onChange({ config: { ...cfg, [key]: value } });
  }
  // Branch helpers — config.branches is a list of {when, next}.
  function patchBranch(i: number, patch: Partial<{ when: string; next: string }>) {
    const branches = ((cfg.branches as Array<{ when: string; next: string }>) ?? []).map((b, j) =>
      j === i ? { ...b, ...patch } : b,
    );
    patchConfig("branches", branches);
  }
  function addBranch() {
    const branches = (cfg.branches as Array<{ when: string; next: string }>) ?? [];
    patchConfig("branches", [...branches, { when: "", next: "" }]);
  }
  function removeBranch(i: number) {
    const branches = ((cfg.branches as Array<{ when: string; next: string }>) ?? []).filter((_, j) => j !== i);
    patchConfig("branches", branches);
  }

  return (
    <div className="border border-border rounded p-3 bg-bg-panel">
      <div className="flex items-center gap-2 mb-2">
        <span className="text-xs text-ink-faint font-mono">[{index + 1}]</span>
        <input
          type="text"
          value={step.id}
          onChange={(e) => onChange({ id: e.target.value })}
          placeholder="step_id"
          className="flex-1 bg-bg border border-border rounded px-2 py-1 text-xs font-mono outline-none focus:border-accent"
        />
        <select
          value={step.type}
          onChange={(e) => onChange({ type: e.target.value as WorkflowV2StepType })}
          className="bg-bg border border-border rounded px-2 py-1 text-xs font-mono outline-none focus:border-accent"
        >
          {STEP_TYPES.map((t) => (
            <option key={t} value={t}>{t}</option>
          ))}
        </select>
        <div className="flex gap-0.5">
          <button
            onClick={() => onMove(-1)}
            disabled={index === 0}
            title="move up"
            className="text-xs px-1.5 py-0.5 rounded border border-border hover:border-accent text-ink-dim disabled:opacity-30"
          >
            ↑
          </button>
          <button
            onClick={() => onMove(1)}
            disabled={index === total - 1}
            title="move down"
            className="text-xs px-1.5 py-0.5 rounded border border-border hover:border-accent text-ink-dim disabled:opacity-30"
          >
            ↓
          </button>
          <button
            onClick={onRemove}
            title="remove"
            className="text-xs px-1.5 py-0.5 rounded border border-border hover:border-err text-ink-dim hover:text-err"
          >
            ✕
          </button>
        </div>
      </div>

      <div className="text-xs text-ink-faint mb-2">{TYPE_HELP[step.type]}</div>

      {/* Type-specific fields */}
      {(step.type === "action") && (
        <label className="block">
          <span className="text-xs text-ink-dim">prompt (supports {"{step_id}"} substitution)</span>
          <textarea
            rows={2}
            value={step.prompt ?? ""}
            onChange={(e) => onChange({ prompt: e.target.value })}
            placeholder="summarize the previous step's output"
            className="mt-1 w-full bg-bg border border-border rounded px-2 py-1 text-xs font-mono outline-none focus:border-accent resize-none"
          />
        </label>
      )}

      {step.type === "validate" && (
        <label className="block">
          <span className="text-xs text-ink-dim">check expression (eval against run state)</span>
          <input
            type="text"
            value={(cfg.check as string) ?? ""}
            onChange={(e) => patchConfig("check", e.target.value)}
            placeholder="len(prev_step) > 0"
            className="mt-1 w-full bg-bg border border-border rounded px-2 py-1 text-xs font-mono outline-none focus:border-accent"
          />
        </label>
      )}

      {step.type === "checkpoint" && (
        <label className="block">
          <span className="text-xs text-ink-dim">approvers (comma-separated; empty = anyone)</span>
          <input
            type="text"
            value={Array.isArray(cfg.approvers) ? (cfg.approvers as string[]).join(", ") : ""}
            onChange={(e) =>
              patchConfig(
                "approvers",
                e.target.value.split(",").map((s) => s.trim()).filter(Boolean),
              )
            }
            placeholder="alice, bob"
            className="mt-1 w-full bg-bg border border-border rounded px-2 py-1 text-xs outline-none focus:border-accent"
          />
        </label>
      )}

      {step.type === "webhook_wait" && (
        <div className="grid grid-cols-2 gap-2">
          <label className="block">
            <span className="text-xs text-ink-dim">webhook_id (shared secret)</span>
            <input
              type="text"
              value={(cfg.webhook_id as string) ?? ""}
              onChange={(e) => patchConfig("webhook_id", e.target.value)}
              placeholder="wh_abc123"
              className="mt-1 w-full bg-bg border border-border rounded px-2 py-1 text-xs font-mono outline-none focus:border-accent"
            />
          </label>
          <label className="block">
            <span className="text-xs text-ink-dim">event_filter (optional)</span>
            <input
              type="text"
              value={(cfg.event_filter as string) ?? ""}
              onChange={(e) => patchConfig("event_filter", e.target.value)}
              placeholder="build.success"
              className="mt-1 w-full bg-bg border border-border rounded px-2 py-1 text-xs font-mono outline-none focus:border-accent"
            />
          </label>
        </div>
      )}

      {step.type === "email_wait" && (
        <div className="grid grid-cols-2 gap-2">
          <label className="block">
            <span className="text-xs text-ink-dim">from_filter (substring)</span>
            <input
              type="text"
              value={(cfg.from_filter as string) ?? ""}
              onChange={(e) => patchConfig("from_filter", e.target.value)}
              placeholder="@company.com"
              className="mt-1 w-full bg-bg border border-border rounded px-2 py-1 text-xs outline-none focus:border-accent"
            />
          </label>
          <label className="block">
            <span className="text-xs text-ink-dim">subject_filter (substring)</span>
            <input
              type="text"
              value={(cfg.subject_filter as string) ?? ""}
              onChange={(e) => patchConfig("subject_filter", e.target.value)}
              placeholder="[APPROVE]"
              className="mt-1 w-full bg-bg border border-border rounded px-2 py-1 text-xs outline-none focus:border-accent"
            />
          </label>
        </div>
      )}

      {step.type === "branch" && (
        <div className="space-y-2">
          <div className="text-xs text-ink-dim">
            branches — first truthy <code>when</code> wins; jump to its <code>next</code> step id
          </div>
          {(cfg.branches as Array<{ when: string; next: string }> ?? []).map((b, i) => (
            <div key={i} className="grid grid-cols-[1fr_1fr_auto] gap-2">
              <input
                type="text"
                value={b.when}
                onChange={(e) => patchBranch(i, { when: e.target.value })}
                placeholder="x < 10"
                className="bg-bg border border-border rounded px-2 py-1 text-xs font-mono outline-none focus:border-accent"
              />
              <input
                type="text"
                value={b.next}
                onChange={(e) => patchBranch(i, { next: e.target.value })}
                placeholder="small_branch_step"
                className="bg-bg border border-border rounded px-2 py-1 text-xs font-mono outline-none focus:border-accent"
              />
              <button
                type="button"
                onClick={() => removeBranch(i)}
                className="text-xs px-1.5 py-0.5 rounded border border-border hover:border-err text-ink-dim hover:text-err"
                title="remove branch"
              >✕</button>
            </div>
          ))}
          <div className="flex gap-2">
            <button
              type="button"
              onClick={addBranch}
              className="text-xs px-2 py-0.5 rounded border border-border hover:border-accent text-ink-dim hover:text-accent"
            >+ branch</button>
            <label className="flex-1">
              <span className="text-xs text-ink-dim">default_next (optional step id)</span>
              <input
                type="text"
                value={(cfg.default_next as string) ?? ""}
                onChange={(e) => patchConfig("default_next", e.target.value)}
                placeholder="fall_through_step"
                className="mt-0.5 w-full bg-bg border border-border rounded px-2 py-1 text-xs font-mono outline-none focus:border-accent"
              />
            </label>
          </div>
        </div>
      )}

      {step.type === "loop" && (
        <div className="space-y-2">
          <label className="block">
            <span className="text-xs text-ink-dim">body (comma-separated step ids to iterate)</span>
            <input
              type="text"
              value={Array.isArray(cfg.body) ? (cfg.body as string[]).join(", ") : ""}
              onChange={(e) =>
                patchConfig(
                  "body",
                  e.target.value.split(",").map((s) => s.trim()).filter(Boolean),
                )
              }
              placeholder="warmup, main_work"
              className="mt-1 w-full bg-bg border border-border rounded px-2 py-1 text-xs font-mono outline-none focus:border-accent"
            />
          </label>
          <div className="grid grid-cols-2 gap-2">
            <label className="block">
              <span className="text-xs text-ink-dim">while (expr; iter var = current count)</span>
              <input
                type="text"
                value={(cfg.while as string) ?? ""}
                onChange={(e) => patchConfig("while", e.target.value)}
                placeholder="iter &lt; 5"
                className="mt-1 w-full bg-bg border border-border rounded px-2 py-1 text-xs font-mono outline-none focus:border-accent"
              />
            </label>
            <label className="block">
              <span className="text-xs text-ink-dim">max_iterations (default 100)</span>
              <input
                type="number"
                min={1}
                value={(cfg.max_iterations as number) ?? ""}
                onChange={(e) => patchConfig("max_iterations", Number(e.target.value))}
                placeholder="100"
                className="mt-1 w-full bg-bg border border-border rounded px-2 py-1 text-xs font-mono outline-none focus:border-accent"
              />
            </label>
          </div>
        </div>
      )}

      {/* Common: condition */}
      <label className="block mt-2">
        <span className="text-xs text-ink-dim">condition (skip when falsy; optional)</span>
        <input
          type="text"
          value={step.condition ?? ""}
          onChange={(e) => onChange({ condition: e.target.value || null })}
          placeholder="state.should_run == true"
          className="mt-1 w-full bg-bg border border-border rounded px-2 py-1 text-xs font-mono outline-none focus:border-accent"
        />
      </label>
    </div>
  );
}
