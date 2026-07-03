/**
 * Team Activity store (Phase I.C.2) — polls the I.C.1 backend
 * `/team/activity` endpoint and caches per-project merged events
 * sorted ascending by ts.
 *
 * The store exposes a `poll(profile, pid)` action; the UI (I.C.3) wires
 * up the setInterval cadence and decides which projects to track. The
 * store doesn't own auth state — pass the TenantProfile to each poll.
 *
 * Cursor semantics: `since[pid]` holds the ts of the last event seen.
 * Each poll passes it as `?since=` so the backend only returns newer
 * events. On a successful poll, merged events are sorted ascending and
 * the cursor advances to the highest ts.
 *
 * Dedup strategy: backend doesn't expose a stable event id, so we
 * dedup by the composite key `session_id|ts|type`. Same-ts ties happen
 * under heavy write — the dedup keeps the merged list stable across
 * re-polls.
 *
 * Busy guard: a per-pid boolean flag prevents concurrent polls from
 * piling up if a fetch is in flight. Late-arriving polls are skipped
 * (the next interval tick will catch up).
 */
import { create } from "zustand";
import { fetchTeamActivity } from "./api";
import type { TenantProfile, TeamEvent } from "./types";

interface TeamActivityState {
  // Per-project list of merged events, sorted ascending by ts.
  perProjectActivity: Record<string, TeamEvent[]>;
  // Per-project cursor: the ts of the last event seen. Next poll passes
  // this as `?since=` to get only new events.
  since: Record<string, string>;
  // Per-project busy flag so the UI can show a loading indicator and
  // we can short-circuit concurrent polls.
  busy: Record<string, boolean>;
  // Per-project error from the last poll (cleared on success).
  error: Record<string, string | null>;
  // Poll one project. Merges new events since the last cursor into
  // perProjectActivity[pid], updates the cursor, and sorts. Safe to
  // call from setInterval.
  poll: (profile: TenantProfile, pid: string) => Promise<void>;
  // Clear state for a project (call on unmount or project switch).
  reset: (pid: string) => void;
}

function dedupKey(e: TeamEvent): string {
  // Enrich with a content facet so two same-session events that share
  // a ts tick AND a type (e.g. two consecutive text deltas written
  // within the same second-resolution ISO stamp) don't collapse.
  // Backend has no stable event id, so this is best-effort.
  const content = (e.text ?? e.tool_use_id ?? e.message ?? "") as string;
  return `${e.session_id}|${e.ts}|${e.type}|${content}`;
}

function sortByTsAsc(a: TeamEvent, b: TeamEvent): number {
  return a.ts < b.ts ? -1 : a.ts > b.ts ? 1 : 0;
}

export const useTeamActivity = create<TeamActivityState>((set, get) => ({
  perProjectActivity: {},
  since: {},
  busy: {},
  error: {},

  poll: async (profile, pid) => {
    // Busy guard: don't pile up concurrent polls for the same pid.
    // The next interval tick will catch up.
    if (get().busy[pid]) return;
    set((s) => ({ busy: { ...s.busy, [pid]: true } }));
    try {
      const cursor = get().since[pid];
      const { events } = await fetchTeamActivity(profile, pid, {
        since: cursor,
        limit: 200,
      });
      set((s) => {
        const existing = s.perProjectActivity[pid] ?? [];
        const seen = new Set(existing.map(dedupKey));
        const merged = [...existing];
        for (const ev of events) {
          const key = dedupKey(ev);
          if (!seen.has(key)) {
            seen.add(key);
            merged.push(ev);
          }
        }
        merged.sort(sortByTsAsc);
        const newCursor =
          merged.length > 0 ? merged[merged.length - 1].ts : cursor;
        return {
          perProjectActivity: { ...s.perProjectActivity, [pid]: merged },
          since: { ...s.since, [pid]: newCursor },
          busy: { ...s.busy, [pid]: false },
          error: { ...s.error, [pid]: null },
        };
      });
    } catch (e) {
      set((s) => ({
        busy: { ...s.busy, [pid]: false },
        error: {
          ...s.error,
          [pid]: e instanceof Error ? e.message : String(e),
        },
      }));
    }
  },

  reset: (pid) =>
    set((s) => {
      const { [pid]: _a, ...restActivity } = s.perProjectActivity;
      const { [pid]: _b, ...restSince } = s.since;
      const { [pid]: _c, ...restBusy } = s.busy;
      const { [pid]: _d, ...restError } = s.error;
      return {
        perProjectActivity: restActivity,
        since: restSince,
        busy: restBusy,
        error: restError,
      };
    }),
}));
