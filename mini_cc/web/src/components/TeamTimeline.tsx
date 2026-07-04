import { useMemo, useState } from "react";
import { useTeamActivity } from "../lib/teamActivity";
import {
  isNavigableSession,
  mergeConsecutiveTexts,
  sessionLabel,
  shortTs,
  speakerLabel,
  summarizeEvent,
  type RenderItem,
} from "../lib/teamEvent";
import type { TeamEvent } from "../lib/types";

// Stable empty array reference for the activity selector. Returning a
// fresh `[]` literal from the selector on every render (when pid has no
// activity yet) makes useSyncExternalStore see a "changed" snapshot and
// re-render forever; a module-level constant keeps the snapshot stable.
const EMPTY: TeamEvent[] = [];

/**
 * TeamTimeline (Phase I.C.4)
 *
 * Main-content component for the "team" tab. Renders the merged activity
 * feed (teammate + lead events) sorted ascending by ts as a vertical
 * timeline, newest-first. The accompanying sidebar filter (rendered
 * inline in Workspace) toggles which sessions appear.
 *
 * Navigation behavior: clicking an event navigates the chat pane to
 * that event's session via setSid — but only for lead sessions (which
 * live in the user's session list). Teammate sessions aren't in the
 * list, so clicks on those are no-ops.
 */
export default function TeamTimeline({
  pid,
  setSid,
}: {
  pid: string;
  sid: string | null;
  setSid: (s: string) => void;
}) {
  const activity = useTeamActivity(
    (s) => s.perProjectActivity[pid] ?? EMPTY,
  );

  // Build the set of distinct session_ids seen in the feed. Memoized so
  // toggling a checkbox doesn't re-derive the list.
  const sessions = useMemo(() => {
    const seen = new Map<string, string>(); // id -> label
    for (const e of activity) {
      if (!seen.has(e.session_id)) {
        seen.set(e.session_id, sessionLabel(e.session_id));
      }
    }
    return Array.from(seen.entries()); // [sessionId, label]
  }, [activity]);

  // Default: every session visible. Use sessionId as the key so the
  // state survives a feed refresh that re-orders sessions.
  const [visible, setVisible] = useState<Record<string, boolean>>({});
  const isVisible = (sessionId: string) =>
    visible[sessionId] === undefined ? true : visible[sessionId];

  // Merge BEFORE reversing. The activity stream is sorted ascending by
  // ts, so walking it in that order yields text chunks in their natural
  // stream order — merging here produces bubbles whose concatenated
  // text reads left-to-right. Reversing first (the old approach) walked
  // chunks newest-first, so each bubble's text came out reversed and
  // read right-to-left. After merging on the ascending stream, we
  // reverse the resulting render items for newest-first display.
  const items = useMemo(() => {
    const ascending = activity.filter((e) => isVisible(e.session_id));
    return mergeConsecutiveTexts(ascending).reverse();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activity, visible]);

  function onClickEvent(sessionId: string) {
    if (isNavigableSession(sessionId)) {
      setSid(sessionId);
    }
  }

  if (activity.length === 0) {
    return (
      <div className="flex-1 flex items-center justify-center p-6 text-sm text-ink-dim">
        no team activity yet — spawn a teammate with{" "}
        <span className="font-mono ml-1">/agents spawn &lt;name&gt; &lt;role&gt;</span>
      </div>
    );
  }

  return (
    <div className="flex-1 overflow-auto p-4">
      {/* Inline filter (acts as the sidebar-equivalent when the sidebar
          is on a different tab. Also exposed via the Workspace sidebar
          for the team tab; keep this here so the component is
          self-sufficient when used standalone.) */}
      <FilterChecklist
        sessions={sessions}
        isVisible={isVisible}
        onToggle={(sid2) =>
          setVisible((s) => ({ ...s, [sid2]: !isVisible(sid2) }))
        }
      />

      <div className="mt-4 space-y-1">
        {items.map((item, i) => {
          const sessionId =
            item.kind === "merged_text" ? item.sessionId : item.session_id;
          const navigable = isNavigableSession(sessionId);
          const ts = item.kind === "merged_text" ? item.ts : item.ts;
          // Speaker = who actually said/did this. For merged_text bubbles
          // and most events the host session is the speaker, but
          // teammate_message events are emitted into the LEAD session by
          // a teammate — speakerLabel picks the right name.
          const speaker =
            item.kind === "merged_text"
              ? sessionLabel(sessionId)
              : speakerLabel(item as TeamEvent);
          return (
            <button
              key={`${ts}-${i}`}
              onClick={() => onClickEvent(sessionId)}
              disabled={!navigable}
              title={
                navigable
                  ? `jump to session ${sessionId}`
                  : "teammate sessions aren't navigable from here"
              }
              className={
                "w-full text-left flex gap-2 items-start border-l-2 pl-3 py-1 rounded-r " +
                (navigable
                  ? "border-accent/40 hover:bg-bg-hover cursor-pointer"
                  : "border-border cursor-not-allowed opacity-80")
              }
            >
              <span className="text-ink-faint shrink-0 font-mono text-[10px] pt-0.5">
                {shortTs(ts)}
              </span>
              <span
                className={
                  "text-[10px] px-1.5 py-0.5 rounded uppercase font-semibold shrink-0 " +
                  "bg-bg-hover text-ink-dim"
                }
              >
                {speaker}
              </span>
              {item.kind === "merged_text" ? (
                <span className="text-ink whitespace-pre-wrap break-words flex-1 min-w-0">
                  {item.text}
                </span>
              ) : (
                <span className="text-ink whitespace-pre-wrap break-words flex-1 min-w-0">
                  {summarizeEvent(item as TeamEvent)}
                </span>
              )}
            </button>
          );
        })}
      </div>
    </div>
  );
}

// Small checklist of sessions (teammates + lead) used to filter the
// timeline. Rendered as controlled checkboxes keyed by session_id.
function FilterChecklist({
  sessions,
  isVisible,
  onToggle,
}: {
  sessions: Array<[string, string]>;
  isVisible: (sid: string) => boolean;
  onToggle: (sid: string) => void;
}) {
  if (sessions.length === 0) return null;
  return (
    <div className="flex flex-wrap gap-2 text-xs">
      {sessions.map(([sid, label]) => (
        <label
          key={sid}
          className="flex items-center gap-1 px-2 py-1 rounded border border-border hover:bg-bg-hover cursor-pointer"
        >
          <input
            type="checkbox"
            checked={isVisible(sid)}
            onChange={() => onToggle(sid)}
          />
          <span>{label}</span>
        </label>
      ))}
    </div>
  );
}
