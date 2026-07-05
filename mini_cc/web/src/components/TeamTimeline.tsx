import { useMemo, useState } from "react";
import { useTeamActivity } from "../lib/teamActivity";
import {
  isMergedText,
  isNavigableSession,
  mergeConsecutiveTexts,
  sessionLabel,
  shortTs,
  speakerLabel,
  summarizeEvent,
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

  // Build the set of distinct SPEAKERS seen in the feed — not raw
  // session_ids. Teammate activity often reaches the lead session as a
  // `teammate_message` event (session_id = the lead's, `from` = the
  // teammate name), so a session_id filter collapses every teammate
  // into "lead". speakerLabel follows the `from` field for those events
  // (and falls back to sessionLabel otherwise), so each teammate stays
  // its own checkbox. Memoized so toggling doesn't re-derive the list.
  const speakers = useMemo(() => {
    const seen = new Set<string>();
    for (const e of activity) {
      seen.add(speakerLabel(e));
    }
    return Array.from(seen); // speaker labels, insertion order
  }, [activity]);

  // Default: every speaker visible. Keyed by speaker label so the
  // state survives a feed refresh that re-orders speakers.
  const [visible, setVisible] = useState<Record<string, boolean>>({});
  const isVisible = (speaker: string) =>
    visible[speaker] === undefined ? true : visible[speaker];

  // Merge BEFORE reversing. The activity stream is sorted ascending by
  // ts, so walking it in that order yields text chunks in their natural
  // stream order — merging here produces bubbles whose concatenated
  // text reads left-to-right. Reversing first (the old approach) walked
  // chunks newest-first, so each bubble's text came out reversed and
  // read right-to-left. After merging on the ascending stream, we
  // reverse the resulting render items for newest-first display.
  const items = useMemo(() => {
    const ascending = activity.filter((e) => isVisible(speakerLabel(e)));
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
        speakers={speakers}
        isVisible={isVisible}
        onToggle={(sp) =>
          setVisible((s) => ({ ...s, [sp]: !isVisible(sp) }))
        }
      />

      <div className="mt-4 space-y-1">
        {items.map((item, i) => {
          // Use the type guard (not a raw `item.kind === ...` check) so the
          // RenderItem union actually narrows — see teamEvent.ts:isMergedText.
          const merged = isMergedText(item);
          const sessionId = merged ? item.sessionId : item.session_id;
          const navigable = isNavigableSession(sessionId);
          const ts = item.ts; // both variants expose ts: string
          // Speaker = who actually said/did this. For merged_text bubbles
          // and most events the host session is the speaker, but
          // teammate_message events are emitted into the LEAD session by
          // a teammate — speakerLabel picks the right name.
          const speaker = merged
            ? sessionLabel(sessionId)
            : speakerLabel(item);
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
              {merged ? (
                <span className="text-ink whitespace-pre-wrap break-words flex-1 min-w-0">
                  {item.text}
                </span>
              ) : (
                <span className="text-ink whitespace-pre-wrap break-words flex-1 min-w-0">
                  {summarizeEvent(item)}
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
  speakers,
  isVisible,
  onToggle,
}: {
  speakers: string[];
  isVisible: (sp: string) => boolean;
  onToggle: (sp: string) => void;
}) {
  if (speakers.length === 0) return null;
  return (
    <div className="flex flex-wrap gap-2 text-xs">
      {speakers.map((sp) => (
        <label
          key={sp}
          className="flex items-center gap-1 px-2 py-1 rounded border border-border hover:bg-bg-hover cursor-pointer"
        >
          <input
            type="checkbox"
            checked={isVisible(sp)}
            onChange={() => onToggle(sp)}
          />
          <span>{sp}</span>
        </label>
      ))}
    </div>
  );
}
