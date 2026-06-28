import type { TenantProfile, WorkflowV2Definition, WorkflowV2Run } from "../../lib/types";

/**
 * Left pane of the Workflow V2 page: definitions list + filtered runs.
 *
 * Two collapsible sections:
 *   - Definitions: pick one to see its runs; edit/create via buttons
 *   - Runs: scoped to the selected definition; click to inspect
 *
 * Status badges give at-a-glance state without forcing the user to
 * click into each run.
 */
interface Props {
  profile: TenantProfile;
  pid: string;
  defs: WorkflowV2Definition[];
  runs: WorkflowV2Run[];
  selectedDefId: string | null;
  selectedRunId: string | null;
  loading: boolean;
  onSelectDef: (id: string) => void;
  onSelectRun: (id: string) => void;
  onNewDefinition: () => void;
  onEditDefinition: (id: string) => void;
}

const STATUS_BADGE: Record<string, string> = {
  pending: "bg-gray-500/20 text-gray-300",
  running: "bg-blue-500/20 text-blue-300 animate-pulse",
  paused: "bg-amber-500/20 text-amber-300",
  completed: "bg-green-500/20 text-green-300",
  failed: "bg-red-500/20 text-red-300",
  cancelled: "bg-gray-500/20 text-gray-400",
};

const STATUS_GLYPH: Record<string, string> = {
  pending: "○",
  running: "▶",
  paused: "⏸",
  completed: "✓",
  failed: "✗",
  cancelled: "⊘",
};

export default function WorkflowV2LeftPane({
  profile: _profile,
  pid: _pid,
  defs,
  runs,
  selectedDefId,
  selectedRunId,
  loading,
  onSelectDef,
  onSelectRun,
  onNewDefinition,
  onEditDefinition,
}: Props) {
  const visibleRuns = selectedDefId
    ? runs.filter((r) => r.def_id === selectedDefId)
    : runs;

  return (
    <aside className="w-72 shrink-0 border-r border-border bg-bg-panel flex flex-col overflow-hidden">
      {/* Definitions */}
      <div className="flex items-center justify-between px-3 pt-3 pb-1">
        <span className="text-xs text-ink-dim uppercase tracking-wide">
          definitions
        </span>
        <button
          onClick={onNewDefinition}
          title="new definition"
          className="text-sm text-ink-dim hover:text-ink px-1.5 rounded border border-border hover:border-accent"
        >
          ＋
        </button>
      </div>

      <div className="flex-1 overflow-auto px-2 pb-2 space-y-0.5">
        {loading && defs.length === 0 && (
          <div className="text-xs text-ink-faint px-2 py-1">loading…</div>
        )}
        {!loading && defs.length === 0 && (
          <div className="text-xs text-ink-faint px-2 py-2">
            no definitions yet
          </div>
        )}
        {defs.map((d) => {
          const isSelected = d.def_id === selectedDefId;
          return (
            <div
              key={d.def_id}
              className={`group flex items-center rounded ${
                isSelected ? "bg-bg-hover" : "hover:bg-bg-hover/50"
              }`}
            >
              <button
                onClick={() => onSelectDef(d.def_id)}
                className="flex-1 text-left px-2 py-1.5 min-w-0"
              >
                <div className="text-sm text-ink truncate">{d.name}</div>
                <div className="text-xs text-ink-faint truncate">
                  v{d.version} · {d.steps.length} steps
                </div>
              </button>
              <button
                onClick={() => onEditDefinition(d.def_id)}
                title="edit"
                className="opacity-0 group-hover:opacity-100 text-xs text-ink-dim hover:text-ink px-2"
              >
                ✎
              </button>
            </div>
          );
        })}
      </div>

      {/* Runs (scoped to selected def) */}
      <div className="border-t border-border flex flex-col max-h-[50%]">
        <div className="px-3 pt-2 pb-1 text-xs text-ink-dim uppercase tracking-wide">
          runs{selectedDefId ? ` · ${defs.find((d) => d.def_id === selectedDefId)?.name ?? ""}` : ""}
        </div>
        <div className="flex-1 overflow-auto px-2 pb-3 space-y-0.5">
          {visibleRuns.length === 0 && (
            <div className="text-xs text-ink-faint px-2 py-2">
              no runs yet
            </div>
          )}
          {visibleRuns.map((r) => {
            const isSelected = r.run_id === selectedRunId;
            const short = r.run_id.replace(/^wfrun_/, "#");
            return (
              <button
                key={r.run_id}
                onClick={() => onSelectRun(r.run_id)}
                className={`w-full flex items-center gap-2 px-2 py-1.5 rounded text-left ${
                  isSelected ? "bg-bg-hover" : "hover:bg-bg-hover/50"
                }`}
              >
                <span className="font-mono text-xs text-ink-dim">{short}</span>
                <span
                  className={`text-[10px] font-medium px-1.5 py-0.5 rounded ${
                    STATUS_BADGE[r.status] ?? STATUS_BADGE.pending
                  }`}
                >
                  {STATUS_GLYPH[r.status] ?? "○"} {r.status}
                </span>
              </button>
            );
          })}
        </div>
      </div>
    </aside>
  );
}
