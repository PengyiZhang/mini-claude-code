import { useEffect, useState } from "react";
import type {
  TenantProfile,
  WorkflowV2Definition,
  WorkflowV2Run,
  WorkflowV2StepRun,
} from "../../lib/types";
import {
  ApiError,
  cancelWorkflowV2Run,
  driveWorkflowV2Run,
  resolveWorkflowV2Gate,
  startWorkflowV2Run,
} from "../../lib/api";
import { listSessionMetas } from "../../lib/api";
import {
  isTerminalStatus,
  startRunPolling,
  useWorkflowV2,
} from "../../lib/workflowV2Store";

/**
 * Run execution view (middle pane). Chat-like event timeline:
 *   [time] run started
 *   [time] step 1: action
 *     → result preview
 *   [time] step 2: checkpoint
 *     ⏸ awaiting approval
 *     [Approve] [Reject]
 *
 * Inline Approve/Reject buttons appear on the paused gate step.
 */
interface Props {
  profile: TenantProfile;
  pid: string;
  run: WorkflowV2Run;
  def: WorkflowV2Definition | null;
  inspectedStepId: string | null;
  onInspectStep: (id: string) => void;
  onRunChanged: () => void;
  onError: (msg: string) => void;
}

export default function WorkflowV2RunView({
  profile,
  pid,
  run,
  def,
  inspectedStepId,
  onInspectStep,
  onRunChanged,
  onError,
}: Props) {
  const [busy, setBusy] = useState(false);
  const [feedback, setFeedback] = useState("");
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [sessionsLoaded, setSessionsLoaded] = useState(false);

  // Resolve a session to drive through. Pick the most-recently-active
  // one if any; the UI offers a dropdown to switch.
  useEffect(() => {
    let cancelled = false;
    listSessionMetas(profile, pid)
      .then((metas) => {
        if (cancelled) return;
        if (metas.length > 0) setSessionId(metas[0].session_id);
        setSessionsLoaded(true);
      })
      .catch(() => { setSessionsLoaded(true); });
    return () => { cancelled = true; };
  }, [profile, pid]);

  // Polling: while the run is non-terminal, refresh every 2s. This
  // catches both /drive advancement and external webhook/email
  // resolution without needing a dedicated event channel.
  useEffect(() => {
    if (!run || isTerminalStatus(run.status)) return;
    const stop = startRunPolling(profile, pid, run.run_id);
    return stop;
  }, [profile, pid, run.run_id, run.status]);

  const pausedGateStep = run.step_runs.find(
    (s) => s.status === "paused",
  );

  async function startNewRun() {
    if (!def) return;
    setBusy(true);
    try {
      const newRun = await startWorkflowV2Run(profile, pid, def.def_id);
      useWorkflowV2.getState().selectRun(newRun.run_id);
      await onRunChanged();
    } catch (e) {
      onError(e instanceof ApiError ? e.message : (e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  async function drive() {
    if (!sessionId) {
      onError("no session available — create one in the chat tab first");
      return;
    }
    setBusy(true);
    try {
      await driveWorkflowV2Run(profile, pid, run.run_id, sessionId);
      await onRunChanged();
    } catch (e) {
      onError(e instanceof ApiError ? e.message : (e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  async function cancel() {
    if (!confirm(`cancel run ${run.run_id}?`)) return;
    setBusy(true);
    try {
      await cancelWorkflowV2Run(profile, pid, run.run_id);
      await onRunChanged();
    } catch (e) {
      onError(e instanceof ApiError ? e.message : (e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  async function resolveGate(stepId: string, decision: "approve" | "reject") {
    setBusy(true);
    try {
      await resolveWorkflowV2Gate(profile, pid, run.run_id, stepId, {
        decision,
        approver: profile.tenantId,
        feedback: feedback || undefined,
      });
      setFeedback("");
      await onRunChanged();
    } catch (e) {
      onError(e instanceof ApiError ? e.message : (e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="flex-1 flex flex-col min-h-0">
      {/* Header — run id + status + action buttons */}
      <div className="border-b border-border px-4 py-2 flex items-center gap-3 bg-bg-panel">
        <div className="min-w-0 flex-1">
          <div className="text-sm font-mono text-ink truncate">
            {run.run_id}
          </div>
          <div className="text-xs text-ink-faint">
            {def?.name ?? run.def_id} · v{run.def_version}
            {run.started_at && ` · started ${fmtTime(run.started_at)}`}
          </div>
        </div>
        <div className="flex gap-2">
          {run.status === "pending" && (
            <button
              onClick={drive}
              disabled={busy}
              className="text-xs px-3 py-1.5 rounded bg-accent text-white hover:bg-accent-hover disabled:opacity-50"
            >
              ▶ drive
            </button>
          )}
          {run.status === "paused" && !pausedGateStep && (
            <button
              onClick={drive}
              disabled={busy}
              className="text-xs px-3 py-1.5 rounded bg-accent text-white hover:bg-accent-hover disabled:opacity-50"
            >
              ▶ resume
            </button>
          )}
          {!isTerminalStatus(run.status) && (
            <button
              onClick={cancel}
              disabled={busy}
              className="text-xs px-3 py-1.5 rounded border border-border hover:border-err text-ink-dim hover:text-err"
            >
              cancel
            </button>
          )}
          {isTerminalStatus(run.status) && def && (
            <button
              onClick={startNewRun}
              disabled={busy}
              className="text-xs px-3 py-1.5 rounded border border-border hover:border-accent text-ink-dim hover:text-ink"
            >
              ↻ new run
            </button>
          )}
        </div>
      </div>

      {/* No-session banner — action steps dispatch through the AgentLoop,
          which needs a chat session. When the project has none, offer a
          one-click shortcut instead of making the user hunt for the tab. */}
      {sessionsLoaded && !sessionId && !isTerminalStatus(run.status) && (
        <div className="border-b border-border bg-amber-50 dark:bg-amber-900/20 px-4 py-2 flex items-center gap-3">
          <span className="text-xs text-amber-800 dark:text-amber-300 flex-1">
            action steps need a chat session to dispatch — none exist in this project yet
          </span>
          <a
            href={`#/projects/${pid}`}
            className="text-xs px-3 py-1.5 rounded bg-accent text-white hover:bg-accent-hover whitespace-nowrap"
          >
            open chat tab →
          </a>
        </div>
      )}

      {/* Timeline */}
      <div className="flex-1 overflow-auto p-6 space-y-3">
        <TimelineEntry
          time={run.started_at}
          glyph="▶"
          title="run started"
          subtitle={`trigger: ${(run.trigger.type as string) ?? "manual"}`}
        />
        {run.step_runs.map((sr, idx) => (
          <StepTimelineEntry
            key={sr.step_id}
            stepRun={sr}
            stepDef={def?.steps.find((s) => s.id === sr.step_id) ?? null}
            index={idx}
            isInspected={sr.step_id === inspectedStepId}
            onInspect={() => onInspectStep(sr.step_id)}
            isPausedGate={sr.step_id === pausedGateStep?.step_id}
            busy={busy}
            feedback={feedback}
            setFeedback={setFeedback}
            onResolve={(decision) => resolveGate(sr.step_id, decision)}
          />
        ))}
        {run.status === "completed" && (
          <TimelineEntry
            time={run.completed_at}
            glyph="✓"
            title="run completed"
            subtitle=""
            tone="success"
          />
        )}
        {run.status === "failed" && (
          <TimelineEntry
            time={run.completed_at}
            glyph="✗"
            title="run failed"
            subtitle=""
            tone="error"
          />
        )}
        {run.status === "cancelled" && (
          <TimelineEntry
            time={run.completed_at}
            glyph="⊘"
            title="run cancelled"
            subtitle=""
            tone="muted"
          />
        )}
      </div>
    </div>
  );
}

function StepTimelineEntry({
  stepRun,
  stepDef,
  index,
  isInspected,
  onInspect,
  isPausedGate,
  busy,
  feedback,
  setFeedback,
  onResolve,
}: {
  stepRun: WorkflowV2StepRun;
  stepDef: { id: string; type: string; prompt?: string } | null;
  index: number;
  isInspected: boolean;
  onInspect: () => void;
  isPausedGate: boolean;
  busy: boolean;
  feedback: string;
  setFeedback: (s: string) => void;
  onResolve: (decision: "approve" | "reject") => void;
}) {
  const tone =
    stepRun.status === "completed" ? "success" :
    stepRun.status === "failed" ? "error" :
    stepRun.status === "paused" ? "warning" :
    stepRun.status === "running" ? "info" : "muted";

  const glyph =
    stepRun.status === "completed" ? "✓" :
    stepRun.status === "failed" ? "✗" :
    stepRun.status === "paused" ? "⏸" :
    stepRun.status === "running" ? "▶" :
    stepRun.status === "skipped" ? "⏭" : "○";

  const preview = previewOutput(stepRun);

  return (
    <div
      className={`rounded border p-3 cursor-pointer transition-colors ${
        isInspected
          ? "border-accent bg-accent/5"
          : "border-border hover:border-accent/50 bg-bg"
      }`}
      onClick={onInspect}
    >
      <div className="flex items-center gap-2">
        <span className={`text-base ${TONE_TEXT[tone]}`}>{glyph}</span>
        <div className="flex-1 min-w-0">
          <div className="text-sm text-ink">
            <span className="text-ink-faint font-mono">[{index + 1}]</span>{" "}
            <span className="font-mono">{stepRun.step_id}</span>
            {stepDef && (
              <span className="ml-2 text-xs px-1.5 py-0.5 rounded bg-bg-hover text-ink-dim font-mono">
                {stepDef.type}
              </span>
            )}
          </div>
          {stepDef?.prompt && (
            <div className="text-xs text-ink-faint truncate mt-0.5">
              {stepDef.prompt}
            </div>
          )}
        </div>
        {stepRun.status === "running" && (
          <span className="text-xs text-blue-400 animate-pulse">running…</span>
        )}
      </div>

      {/* Output preview */}
      {preview && (
        <div className="mt-2 ml-7 text-xs text-ink-dim font-mono bg-bg-panel rounded p-2 break-words">
          {preview}
        </div>
      )}

      {/* Error */}
      {stepRun.error && (
        <div className="mt-2 ml-7 text-xs text-err bg-err/10 rounded p-2 break-words font-mono">
          {stepRun.error}
        </div>
      )}

      {/* Approve / Reject inline */}
      {isPausedGate && (
        <div
          className="mt-3 ml-7 flex flex-col gap-2"
          onClick={(e) => e.stopPropagation()}
        >
          <input
            type="text"
            placeholder="feedback (optional)"
            value={feedback}
            onChange={(e) => setFeedback(e.target.value)}
            disabled={busy}
            className="text-xs bg-bg border border-border rounded px-2 py-1 outline-none focus:border-accent disabled:opacity-50"
          />
          <div className="flex gap-2">
            <button
              onClick={() => onResolve("approve")}
              disabled={busy}
              className="text-xs px-3 py-1.5 rounded bg-green-600 text-white hover:bg-green-700 disabled:opacity-50"
            >
              ✓ approve
            </button>
            <button
              onClick={() => onResolve("reject")}
              disabled={busy}
              className="text-xs px-3 py-1.5 rounded bg-red-600 text-white hover:bg-red-700 disabled:opacity-50"
            >
              ✗ reject
            </button>
          </div>
        </div>
      )}
    </div>
  );
}

function TimelineEntry({
  time,
  glyph,
  title,
  subtitle,
  tone = "info",
}: {
  time: string | null;
  glyph: string;
  title: string;
  subtitle: string;
  tone?: "info" | "success" | "error" | "warning" | "muted";
}) {
  return (
    <div className="flex items-center gap-3 text-sm">
      <span className={`text-base ${TONE_TEXT[tone]}`}>{glyph}</span>
      <div className="flex-1">
        <span className="text-ink">{title}</span>
        {subtitle && <span className="ml-2 text-xs text-ink-faint">{subtitle}</span>}
      </div>
      {time && <span className="text-xs text-ink-faint">{fmtTime(time)}</span>}
    </div>
  );
}

const TONE_TEXT: Record<string, string> = {
  info: "text-blue-400",
  success: "text-green-400",
  error: "text-red-400",
  warning: "text-amber-400",
  muted: "text-gray-500",
};

function previewOutput(sr: WorkflowV2StepRun): string | null {
  if (!sr.output) return null;
  if (typeof sr.output === "string") return sr.output.slice(0, 240);
  try {
    return JSON.stringify(sr.output).slice(0, 240);
  } catch {
    return null;
  }
}

function fmtTime(iso: string): string {
  try {
    const d = new Date(iso);
    return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
  } catch {
    return iso;
  }
}
