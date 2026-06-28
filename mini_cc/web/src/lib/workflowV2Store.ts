/**
 * Workflow V2 store (W6) — definitions + runs in memory, with
 * per-run polling for live status updates.
 *
 * The backend doesn't expose an SSE stream of workflow events yet;
 * runs advance via POST /drive (synchronous) or external triggers
 * (webhook / email). Polling run state every ~2s catches both
 * paths without a dedicated event channel.
 */
import { create } from "zustand";
import type { TenantProfile, WorkflowV2Definition, WorkflowV2Run } from "./types";
import {
  getWorkflowV2Run,
  listWorkflowV2Defs,
  listWorkflowV2Runs,
} from "./api";

interface WorkflowV2State {
  defs: WorkflowV2Definition[];
  runs: WorkflowV2Run[];
  selectedDefId: string | null;
  selectedRunId: string | null;
  loading: boolean;
  error: string | null;
  load: (profile: TenantProfile, pid: string) => Promise<void>;
  selectDef: (defId: string | null) => void;
  selectRun: (runId: string | null) => void;
  refreshRuns: (profile: TenantProfile, pid: string, defId?: string) => Promise<void>;
  refreshRun: (profile: TenantProfile, pid: string, runId: string) => Promise<void>;
  setError: (msg: string | null) => void;
}

export const useWorkflowV2 = create<WorkflowV2State>((set, get) => ({
  defs: [],
  runs: [],
  selectedDefId: null,
  selectedRunId: null,
  loading: false,
  error: null,

  async load(profile, pid) {
    set({ loading: true, error: null });
    try {
      const [defs, runs] = await Promise.all([
        listWorkflowV2Defs(profile, pid),
        listWorkflowV2Runs(profile, pid),
      ]);
      // Sort runs newest-first by started_at. Untouched / fresh runs
      // have null started_at — those sink to the bottom.
      runs.sort((a, b) => (b.started_at ?? "").localeCompare(a.started_at ?? ""));
      set({ defs, runs, loading: false });
    } catch (e) {
      set({ error: (e as Error).message, loading: false });
    }
  },

  selectDef(defId) {
    set({ selectedDefId: defId });
  },

  selectRun(runId) {
    set({ selectedRunId: runId });
  },

  async refreshRuns(profile, pid, defId) {
    try {
      const runs = await listWorkflowV2Runs(profile, pid, defId);
      runs.sort((a, b) => (b.started_at ?? "").localeCompare(a.started_at ?? ""));
      set({ runs });
    } catch (e) {
      set({ error: (e as Error).message });
    }
  },

  async refreshRun(profile, pid, runId) {
    try {
      const updated = await getWorkflowV2Run(profile, pid, runId);
      set((s) => ({
        runs: s.runs.map((r) => (r.run_id === runId ? updated : r)),
      }));
    } catch {
      // Stale run id (deleted mid-poll) — leave as is; full refresh
      // on next load will reconcile.
    }
  },

  setError(msg) {
    set({ error: msg });
  },
}));

/**
 * Polling hook. While the selected run is in a non-terminal state
 * (running/paused/pending), refresh its state every `intervalMs`.
 * Stops when the run reaches a terminal state.
 */
export function startRunPolling(
  profile: TenantProfile,
  pid: string,
  runId: string | null,
  intervalMs = 2000,
): () => void {
  if (!runId) return () => {};
  let cancelled = false;
  const tick = async () => {
    if (cancelled || !runId) return;
    const store = useWorkflowV2.getState();
    await store.refreshRun(profile, pid, runId);
    const cur = useWorkflowV2.getState().runs.find((r) => r.run_id === runId);
    if (cur && !isTerminalStatus(cur.status)) {
      setTimeout(tick, intervalMs);
    }
  };
  setTimeout(tick, intervalMs);
  return () => {
    cancelled = true;
  };
}

export function isTerminalStatus(status: string): boolean {
  return status === "completed" || status === "failed" || status === "cancelled";
}
