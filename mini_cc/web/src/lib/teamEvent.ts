import type { TeamEvent } from "./types";

// Shared helpers for rendering TeamEvent payloads. Used by both the
// TeammatesPanel (per-teammate recent activity) and the TeamTimeline
// (merged cross-teammate/lead timeline). Extracted in I.C.4 so the new
// Team tab doesn't duplicate the summarization logic.

// A render item is either a pass-through event (tool_use, tool_result,
// etc.) or a merged_text bubble — multiple consecutive `text` events
// from the same session collapsed into a single readable message.
//
// Why merge: the AgentLoop emits one `text` SSE event per streamed
// chunk (often one chunk per token), and the /send generator persists
// every one of them to events.jsonl. Without merging, the Team tab
// shows one row per chunk — each token on its own line, completely
// unreadable. Merging at the render layer (rather than the persistence
// layer) also fixes legacy logs already on disk.
// A merged_text bubble. Named (not inlined in RenderItem) so the
// isMergedText guard below can reference it without restating the shape.
export interface MergedTextItem {
  kind: "merged_text";
  sessionId: string;
  ts: string;
  text: string;
}

export type RenderItem = MergedTextItem | TeamEvent;

// Narrows a RenderItem to its merged_text variant. A raw
// `item.kind === "merged_text"` check does NOT narrow at the call site:
// TeamEvent carries an index signature (`[key: string]: unknown`), so its
// `.kind` is `unknown` rather than absent — TypeScript won't treat `kind`
// as a real discriminant and keeps the full union in both branches. A
// user-defined type guard narrows explicitly regardless.
export function isMergedText(item: RenderItem): item is MergedTextItem {
  return item.kind === "merged_text";
}

// Internal XML envelopes the LLM occasionally hallucinates as plain
// text output (it's the format we feed into its context for inbox /
// teammate_messages). When the model erroneously types these back at
// the keyboard, the merger would otherwise join the per-token chunks
// into one giant bubble showing raw `<teammate_messages>[{...}]</...>`.
//
// Detection runs on the FULL merged bubble text, not on individual
// chunks — important because the SSE stream tokenizes aggressively
// (chunks like "<te" + "amm" + "ate" + "_messages" ...), so a single
// chunk almost never contains a complete tag.
//
// We drop the entire bubble if it contains any envelope tag (open OR
// close). Yes, this loses any real text that happened to precede the
// hallucination in the same bubble — but in practice the LLM either
// emits clean text OR hallucinates the envelope, rarely both in the
// same bubble. Reliable hiding of garbage beats surgical preservation.
//
// Cross-bubble case: if a real event (e.g. teammate_message) interrupts
// the hallucination mid-stream, the post-interrupt continuation bubble
// contains a stray `</teammate_messages>` close tag without any open.
// The "any tag → drop" rule catches that tail too.
const HALLUCINATED_ENVELOPE_TAGS = [
  // Internal inbox/teammate wire format that the LLM sees as input
  // context and occasionally echoes back as text. See git history
  // (commit "fix(team): ...") for the original report.
  "teammate_messages",
  "inbox",
  "system_messages",
  "channel_update",
  "notifier",
  // Anthropic tool-call XML syntax. The structured tool_use event is
  // the only legitimate channel for tool calls — if these tags show up
  // as text chunks, the LLM is hallucinating "I'm calling a tool"
  // instead of actually emitting one. Same family of bug as above.
  "function_calls",
  "invoke",
  "parameter",
];

function bubbleHasEnvelopeTag(s: string): boolean {
  for (const tag of HALLUCINATED_ENVELOPE_TAGS) {
    if (s.includes(`<${tag}>`) || s.includes(`</${tag}>`)) return true;
  }
  return false;
}

export function mergeConsecutiveTexts(events: TeamEvent[]): RenderItem[] {
  const out: RenderItem[] = [];
  let buf: { sessionId: string; ts: string; parts: string[] } | null = null;

  const flush = () => {
    if (buf) {
      const text = buf.parts.join("");
      // Drop the entire bubble if it shows signs of being a
      // hallucinated envelope dump (open or close tag anywhere).
      if (text && !bubbleHasEnvelopeTag(text)) {
        out.push({
          kind: "merged_text",
          sessionId: buf.sessionId,
          ts: buf.ts,
          text,
        });
      }
      buf = null;
    }
  };

  for (const e of events) {
    if (e.type === "text" && typeof (e as any).text === "string") {
      if (!buf || buf.sessionId !== e.session_id) {
        flush();
        buf = { sessionId: e.session_id, ts: e.ts, parts: [] };
      }
      buf.parts.push((e as any).text as string);
    } else {
      flush();
      out.push(e);
    }
  }
  flush();
  return out;
}

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
    case "teammate_message": {
      // Teammate-broadcast event (milestone / performance / blocker /
      // plan_approval_request / etc.) routed through the lead session.
      // The actual speaker label is rendered separately (speakerLabel
      // picks the `from` field), so we just summarize the content here.
      // Without this case the event fell through to the default branch
      // and rendered as the useless placeholder "[teammate_message]".
      const content =
        (typeof e.content === "string" && e.content) ||
        (typeof e.message === "string" && e.message) ||
        "";
      const msgType =
        (typeof e.msg_type === "string" && e.msg_type) || "message";
      if (!content) return `[${msgType}]`;
      return truncate(content, 80);
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

// Pick the speaker label for an event. Most events are emitted INTO a
// session by that session's owner (a `text` event from teammate-alice
// was produced BY alice), so sessionLabel(session_id) is correct.
// But `teammate_message` events are emitted into the LEAD session by
// a teammate (e.g. `_emit_to_lead` writes alice's milestone to the
// lead's events.jsonl with from="alice"). For those, the host label
// ("lead") is misleading — the actual speaker is the `from` field.
// Falls back to sessionLabel when `from` is missing or equals the
// session's own name.
export function speakerLabel(e: TeamEvent): string {
  const fromField = (e as any).from as string | undefined;
  if (typeof fromField === "string" && fromField) {
    const host = sessionLabel(e.session_id);
    if (fromField !== host) return fromField;
  }
  return sessionLabel(e.session_id);
}
