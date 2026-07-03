import type { TeamEvent } from "./types";

// Shared helpers for rendering TeamEvent payloads. Used by both the
// TeammatesPanel (per-teammate recent activity) and the TeamTimeline
// (merged cross-teammate/lead timeline). Extracted in I.C.4 so the new
// Team tab doesn't duplicate the summarization logic.

// Truncate to `n` chars, appending … if anything was dropped.
export function truncate(s: string, n: number): string {
  if (s.length <= n) return s;
  return s.slice(0, n) + "…";
}

// Render one TeamEvent as a single-line summary. The event shapes come
// from the I.C.1 backend (mini_cc/server/routes/team.py), which spreads
// the original assistant/event payload fields alongside session_id and
// ts. We coerce defensively since the type is permissive.
export function summarizeEvent(e: TeamEvent): string {
  switch (e.type) {
    case "tool_use": {
      const name = (e.name as string) ?? "tool";
      const input = e.input as Record<string, unknown> | undefined;
      // Try a few common fields, fall back to JSON of the whole input.
      const preview = String(
        (typeof input?.command === "string" ? input.command : "") ||
          (typeof input?.path === "string" ? input.path : "") ||
          (typeof input?.query === "string" ? input.query : "") ||
          (typeof input?.pattern === "string" ? input.pattern : "") ||
          JSON.stringify(input ?? {}),
      );
      return `${name} ${truncate(preview, 80)}`;
    }
    case "tool_result": {
      const content =
        (typeof e.content === "string" && e.content) ||
        (typeof e.message === "string" && e.message) ||
        "";
      return truncate(content, 80);
    }
    case "send_message": {
      const to = (e.to as string) ?? "?";
      const message =
        (typeof e.message === "string" && e.message) ||
        (typeof e.text === "string" && e.text) ||
        "";
      return `→ ${to}: ${truncate(message, 80)}`;
    }
    case "text": {
      const text = typeof e.text === "string" ? e.text : "";
      return truncate(text, 80);
    }
    default:
      return `[${e.type}]`;
  }
}

// Format the ts as HH:MM:SS for compactness. Falls back to the raw
// string on any parse failure (ts comes from backend ISO stamps).
export function shortTs(ts: string): string {
  const t = ts.slice(11, 19); // YYYY-MM-DDTHH:MM:SSZ -> HH:MM:SS
  return t || ts;
}

// Derive a short display label for a session_id. Teammate sessions are
// named `teammate-<name>`; lead sessions use the regular `sess-…`
// shape. Returns "lead" for the latter so the timeline groups cleanly.
export function sessionLabel(sessionId: string): string {
  if (sessionId.startsWith("teammate-")) {
    return sessionId.slice("teammate-".length);
  }
  return "lead";
}

// True if events from this session are navigable from the Team tab —
// i.e. clicking should setSid(session_id). Lead sessions live in the
// user's session list, so they navigate; teammate sessions do not.
export function isNavigableSession(sessionId: string): boolean {
  return !sessionId.startsWith("teammate-");
}
