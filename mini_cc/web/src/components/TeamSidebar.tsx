import { useState } from "react";
import TeamTimeline from "./TeamTimeline";
import TeammatesPanel from "./TeammatesPanel";

/**
 * TeamSidebar
 *
 * The chat page's right sidebar. Folds the old standalone "team" tab
 * into the chat view so the user can watch the whole team — the merged
 * timeline AND the per-teammate roster/inbox — without leaving the
 * session conversation.
 *
 * Layout: a master-collapsible <aside> containing two independently
 * collapsible stacked panes:
 *   ▲ Timeline — <TeamTimeline>: read-only merged narrative of every
 *                teammate + lead event (group-chat-style).
 *   ▼ Roster   — <TeammatesPanel>: per-teammate status pills + expandable
 *                recent activity + inbox with ack/approve actions.
 *
 * Both panes scroll independently; collapsing one lets the other flex
 * to fill the height. The master collapse shrinks the whole sidebar to
 * a thin rail so the chat pane can own the full width when there is
 * nothing to watch.
 */
export default function TeamSidebar({
  pid,
  sid,
  setSid,
}: {
  pid: string;
  sid: string | null;
  setSid: (s: string) => void;
}) {
  const [masterCollapsed, setMasterCollapsed] = useState(false);
  const [timelineCollapsed, setTimelineCollapsed] = useState(false);
  const [rosterCollapsed, setRosterCollapsed] = useState(false);

  if (masterCollapsed) {
    return (
      <aside className="border-l border-border bg-bg-panel w-10 shrink-0 flex flex-col items-center gap-2 py-2">
        <button
          onClick={() => setMasterCollapsed(false)}
          className="text-xs text-ink-dim hover:text-ink px-1.5 py-0.5 rounded hover:bg-bg-hover"
          aria-label="Expand team sidebar"
          title="Expand team"
        >
          ◀
        </button>
        <div
          className="text-[10px] uppercase tracking-wide text-ink-faint"
          style={{ writingMode: "vertical-rl" }}
        >
          team
        </div>
      </aside>
    );
  }

  return (
    <aside className="border-l border-border bg-bg-panel w-80 shrink-0 flex flex-col min-h-0">
      {/* Master header */}
      <div className="flex items-center justify-between px-3 py-2 border-b border-border shrink-0">
        <div className="text-xs uppercase tracking-wide text-ink-dim">Team</div>
        <button
          onClick={() => setMasterCollapsed(true)}
          className="text-xs text-ink-dim hover:text-ink px-1.5 py-0.5 rounded hover:bg-bg-hover"
          aria-label="Collapse team sidebar"
          title="Collapse to rail"
        >
          ▶
        </button>
      </div>

      {/* Timeline pane (top) — merged read-only team narrative */}
      <section
        className={
          "flex flex-col min-h-0 " +
          (timelineCollapsed ? "shrink-0" : "flex-1")
        }
      >
        <PaneHeader
          label="Timeline"
          collapsed={timelineCollapsed}
          onToggle={() => setTimelineCollapsed((v) => !v)}
        />
        {!timelineCollapsed && (
          <div className="flex-1 min-h-0 flex flex-col">
            <TeamTimeline pid={pid} sid={sid} setSid={setSid} />
          </div>
        )}
      </section>

      {/* Roster pane (bottom) — per-teammate status + inbox control */}
      <section
        className={
          "flex flex-col min-h-0 border-t border-border " +
          (rosterCollapsed ? "shrink-0" : "flex-1")
        }
      >
        <PaneHeader
          label="Roster"
          collapsed={rosterCollapsed}
          onToggle={() => setRosterCollapsed((v) => !v)}
        />
        {!rosterCollapsed && (
          <div className="flex-1 min-h-0 overflow-hidden">
            {sid ? (
              <TeammatesPanel pid={pid} sid={sid} />
            ) : (
              <div className="h-full flex items-center justify-center p-4 text-center text-xs text-ink-faint">
                create or pick a session to manage teammates
              </div>
            )}
          </div>
        )}
      </section>
    </aside>
  );
}

function PaneHeader({
  label,
  collapsed,
  onToggle,
}: {
  label: string;
  collapsed: boolean;
  onToggle: () => void;
}) {
  return (
    <button
      onClick={onToggle}
      className="flex items-center gap-1 px-3 py-1.5 text-xs uppercase tracking-wide text-ink-dim hover:bg-bg-hover text-left w-full shrink-0"
      aria-expanded={!collapsed}
      aria-label={collapsed ? `Expand ${label}` : `Collapse ${label}`}
      title={collapsed ? `Expand ${label}` : `Collapse ${label}`}
    >
      <span className="text-[10px] w-3 inline-block text-center">
        {collapsed ? "▶" : "▼"}
      </span>
      <span>{label}</span>
    </button>
  );
}
