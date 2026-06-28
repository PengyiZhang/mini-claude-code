import { useEffect, useState } from "react";
import { useParams } from "react-router-dom";
import TopBar from "../components/TopBar";
import WorkflowV2LeftPane from "../components/workflowV2/LeftPane";
import WorkflowV2RunView from "../components/workflowV2/RunView";
import WorkflowV2StepInspector from "../components/workflowV2/StepInspector";
import DefinitionEditor from "../components/workflowV2/DefinitionEditor";
import { useAuth } from "../lib/store";
import { useWorkflowV2 } from "../lib/workflowV2Store";
import type { WorkflowV2Definition } from "../lib/types";

/**
 * Workflow V2 page (W6). Three-pane layout:
 *
 *   ┌─────────────┬───────────────────────────┬──────────────────┐
 *   │ Definitions │ Run execution view         │ Step inspector    │
 *   │ Runs        │ (chat-like event timeline   │ (inputs/outputs/  │
 *   │             │  + Approve/Reject buttons)  │  schema/approver) │
 *   └─────────────┴───────────────────────────┴──────────────────┘
 *
 * Plus a slide-out Definition editor for authoring new workflows.
 *
 * Backed by the W1-W5 endpoints + the W6 /drive route. Polling
 * catches drive advancement + external webhook/email resolution.
 */
export default function WorkflowV2() {
  const { pid = "" } = useParams();
  const profile = useAuth((s) => s.current())!;
  const { defs, runs, selectedDefId, selectedRunId, loading, error, load,
          selectRun } = useWorkflowV2();
  const [editorOpen, setEditorOpen] = useState(false);
  const [editorDef, setEditorDef] = useState<WorkflowV2DefEditorTarget | null>(null);

  // Selected run object — kept in sync with store so polling updates
  // flow into RunView without a re-fetch.
  const selectedRun = runs.find((r) => r.run_id === selectedRunId) ?? null;
  const selectedDef = defs.find((d) => d.def_id === selectedDefId) ?? null;

  // Active step = the current or last-touched step in the run.
  const activeStepId = (() => {
    if (!selectedRun) return null;
    if (selectedRun.status === "paused" || selectedRun.status === "running") {
      const sr = selectedRun.step_runs[selectedRun.current_step_idx];
      return sr?.step_id ?? null;
    }
    // Terminal — pick the last non-pending step.
    for (let i = selectedRun.step_runs.length - 1; i >= 0; i--) {
      const sr = selectedRun.step_runs[i];
      if (sr.status !== "pending") return sr.step_id;
    }
    return null;
  })();
  const [inspectedStepId, setInspectedStepId] = useState<string | null>(null);
  const inspectedStep = inspectedStepId ?? activeStepId;

  useEffect(() => {
    void load(profile, pid);
  }, [profile, pid, load]);

  // Auto-select first def on initial load so the left pane isn't empty.
  useEffect(() => {
    if (!selectedDefId && defs.length > 0) {
      useWorkflowV2.getState().selectDef(defs[0].def_id);
    }
  }, [defs, selectedDefId]);

  function openEditorForNew() {
    setEditorDef({ mode: "new" });
    setEditorOpen(true);
  }
  function openEditorForExisting(defId: string) {
    const d = defs.find((x) => x.def_id === defId);
    if (!d) return;
    setEditorDef({ mode: "edit", def: d });
    setEditorOpen(true);
  }

  return (
    <div className="h-screen overflow-hidden flex flex-col">
      <TopBar title={`${pid} · workflow`} />
      <div className="flex-1 flex min-h-0">
        <WorkflowV2LeftPane
          profile={profile}
          pid={pid}
          defs={defs}
          runs={runs}
          selectedDefId={selectedDefId}
          selectedRunId={selectedRunId}
          loading={loading}
          onSelectDef={(id) => useWorkflowV2.getState().selectDef(id)}
          onSelectRun={(id) => selectRun(id)}
          onNewDefinition={openEditorForNew}
          onEditDefinition={openEditorForExisting}
        />

        <main className="flex-1 flex min-w-0">
          <section className="flex-1 flex flex-col min-w-0 border-r border-border">
            {error && (
              <div className="text-sm text-err bg-err/10 border-b border-err/40 px-4 py-2">
                {error}
              </div>
            )}
            {selectedRun ? (
              <WorkflowV2RunView
                profile={profile}
                pid={pid}
                run={selectedRun}
                def={selectedDef ?? defs.find((d) => d.def_id === selectedRun.def_id) ?? null}
                inspectedStepId={inspectedStep}
                onInspectStep={setInspectedStepId}
                onRunChanged={() => useWorkflowV2.getState().refreshRuns(profile, pid)}
                onError={(msg) => useWorkflowV2.getState().setError(msg)}
              />
            ) : (
              <div className="flex-1 flex items-center justify-center text-sm text-ink-dim p-8">
                {defs.length === 0
                  ? "no workflow definitions yet — click ＋ new definition to author one"
                  : "select a run from the left to inspect its execution"}
              </div>
            )}
          </section>

          <aside className="w-96 shrink-0 bg-bg-panel overflow-auto">
            <WorkflowV2StepInspector
              run={selectedRun}
              def={selectedDef ?? (selectedRun ? defs.find((d) => d.def_id === selectedRun.def_id) ?? null : null)}
              stepId={inspectedStep}
            />
          </aside>
        </main>
      </div>

      {editorOpen && editorDef && (
        <DefinitionEditor
          profile={profile}
          pid={pid}
          target={editorDef}
          onClose={() => setEditorOpen(false)}
          onSaved={() => {
            setEditorOpen(false);
            void load(profile, pid);
          }}
        />
      )}
    </div>
  );
}

export type WorkflowV2DefEditorTarget =
  | { mode: "new" }
  | { mode: "edit"; def: WorkflowV2EditorDefShape };

// Local type alias to avoid importing the full Definition type into
// the page (keeps the prop-drilling surface small).
type WorkflowV2EditorDefShape = WorkflowV2Definition;
