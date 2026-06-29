import { useState } from "react";
import type { ChatActivity, ChatMessage } from "../lib/store";
import { MarkdownRenderer } from './MarkdownRenderer'
import { lineDiff, diffStats } from "../lib/diff"
import BackgroundTile from "./BackgroundTile"
import { useAuth, useSessionNav } from "../lib/store"

export default function MessageBubble({ msg }: { msg: ChatMessage }) {
  if (msg.role === "user") {
    return (
      <div className="flex justify-end">
        <div className="bg-accent/15 border border-accent/30 text-ink rounded-lg rounded-br-sm px-4 py-2 max-w-[80%] whitespace-pre-wrap break-words">
          {msg.text}
        </div>
      </div>
    );
  }

  return (
    <div className="flex gap-3">
      <div className="size-8 rounded-md bg-gradient-to-br from-accent to-indigo-400 shrink-0 mt-0.5" />
      <div className="flex-1 space-y-2 min-w-0">
        {msg.activities?.map((a) => (
          <Activity key={a.id} msg={msg} activity={a} />
        ))}
        {(msg.text || msg.streaming) && (
          <div className={`break-words text-ink ${msg.streaming ? "streaming-cursor" : ""}`}>
            <MarkdownRenderer content={msg.text} />
          </div>
        )}
        {msg.notices?.map((n, i) => (
          <div
            key={i}
            className="text-xs text-ink-dim bg-bg-hover border border-border rounded px-2 py-1"
          >
            {n}
          </div>
        ))}
        {msg.error && (
          <div className="text-sm text-err bg-err/10 border border-err/40 rounded px-3 py-2">
            {msg.error}
          </div>
        )}
      </div>
    </div>
  );
}

import { useChat } from "../lib/store";

function Activity({
  msg,
  activity,
}: {
  msg: ChatMessage;
  activity: ChatActivity;
}) {
  // We use the global store's toggleActivity with the message key embedded
  // in the parent — but here we only have the message. Instead of re-keying,
  // we look up the message in the store directly by identity: pass a
  // closure via a custom event. Simpler: lift via toggleActivityByKey.
  // For simplicity here, dispatch through window event.
  // (See store.ts — toggleActivity takes a key.)
  const key = (msg as unknown as { __key?: string }).__key;
  const toggle = useChat((s) => s.toggleActivity);
  // F5.1: distinct icon/badge for the `task` tool so the user can tell
  // at a glance that this turn delegated to a subagent.
  const isSubagent = activity.name === "task";
  const icon = isSubagent ? "🤖" : "🔧";
  const label = isSubagent ? "Subagent" : activity.name;
  return (
    <div className={"border rounded-md text-sm overflow-hidden " +
                    (isSubagent ? "border-accent/40 bg-accent/5"
                                : "border-border bg-bg-card")}>
      <button
        onClick={() => key && toggle(key, activity.id)}
        className="w-full flex items-center gap-2 px-3 py-2 hover:bg-bg-hover text-left"
      >
        <span className="text-xs text-ink-dim">{activity.expanded ? "▼" : "▶"}</span>
        <span className="text-accent">{icon} {label}</span>
        <span className="text-ink-dim text-xs truncate">{summarize(activity.input)}</span>
        {activity.result === undefined ? (
          <span className="ml-auto text-xs text-warn animate-pulse">running…</span>
        ) : (
          <span className="ml-auto text-xs text-ok">✓ done</span>
        )}
      </button>
      {activity.expanded && (
        <div className="border-t border-border p-3 space-y-2 bg-bg">
          <ActivityBody activity={activity} chatKey={key} />
        </div>
      )}
    </div>
  );
}

/**
 * Render an expanded activity's input + result.
 *
 * F4.1 special-cases edit_file/write_file to show a unified diff
 * (input.old vs input.new) instead of the raw JSON. All other tools
 * fall back to JSON input + text result.
 *
 * Long outputs (> COLLAPSE_THRESHOLD lines) render with a "Show more"
 * toggle so the chat scroll doesn't get wrecked by a 10k-line log.
 */
const COLLAPSE_THRESHOLD = 50;

function ActivityBody({ activity, chatKey }: {
  activity: ChatActivity;
  chatKey?: string;
}) {
  // F5.1: subagent dispatch via the `task` tool. Render as a "drawer"
  // with a distinct Subagent badge and a description-first layout so
  // the user can tell at a glance that this turn delegated work.
  if (activity.name === "task") {
    return <SubagentDrawer activity={activity} />;
  }
  const isEdit = activity.name === "edit_file" || activity.name === "write_file";
  if (isEdit) {
    const oldText = String(activity.input?.old ?? "");
    const newText = String(activity.input?.new ?? activity.input?.content ?? "");
    const path = String(activity.input?.path ?? "");
    const stats = diffStats(oldText, newText);
    const lines = lineDiff(oldText, newText);
    return (
      <div className="space-y-2">
        <div className="text-xs text-ink-dim">
          {path && <span className="font-mono">{path} </span>}
          <span className="text-ok">+{stats.adds}</span>{" "}
          <span className="text-err">-{stats.dels}</span>
        </div>
        <DiffView lines={lines} />
      </div>
    );
  }
  // F4.2: when the result carries a "[Background task bg_xxx started]"
  // marker, render the live-updating tile instead of the static result.
  const bgId = parseBgId(activity.result);
  if (bgId) {
    return <BackgroundTileBody activity={activity} bgId={bgId} chatKey={chatKey} />;
  }
  return (
    <>
      <div>
        <div className="text-xs text-ink-dim mb-1">input</div>
        <pre className="text-xs font-mono whitespace-pre-wrap break-all bg-bg-card border border-border rounded p-2">
          {JSON.stringify(activity.input, null, 2)}
        </pre>
      </div>
      {activity.result !== undefined && (
        <div>
          <div className="text-xs text-ink-dim mb-1">result</div>
          <CollapsibleOutput text={activity.result} />
        </div>
      )}
    </>
  );
}

function SubagentDrawer({ activity }: { activity: ChatActivity }) {
  // F5.1: drawer-style render for subagent dispatches.
  // - description is the headline (that's the task the parent handed off)
  // - result is the subagent's final summary, including a trailing
  //   `[subagent_session_id: <sid>]` marker emitted by tools/subagent.py
  // Sibling tool_use/tool_result activities emitted by the subagent
  // are still rendered as their own activity cards in the parent
  // bubble; this drawer is just the wrapper for the dispatch itself.
  const jumpTo = useSessionNav((s) => s.jumpTo);
  const description = String(activity.input?.description ?? "");
  const rawResult = activity.result;
  const isRunning = rawResult === undefined;
  // Split the trailing [subagent_session_id: ...] marker off the
  // summary so we can render it as a clickable chip instead of plain
  // text (P0-6 goal: surface subagent session_id; this completes it
  // by making it actionable, not just visible).
  const m = (typeof rawResult === "string")
    ? rawResult.match(/\[subagent_session_id:\s*([^\]\s]+)\s*\]/)
    : null;
  const sid = m?.[1] ?? null;
  const summary = (typeof rawResult === "string" && m)
    ? rawResult.slice(0, m.index).replace(/\s+$/, "")
    : rawResult;
  return (
    <div className="border border-accent/40 bg-accent/5 rounded-md overflow-hidden">
      <div className="flex items-center gap-2 px-3 py-2 border-b border-accent/30 bg-accent/10">
        <span className="text-xs">🤖</span>
        <span className="text-xs font-semibold uppercase tracking-wide text-accent">
          Subagent
        </span>
        {isRunning && (
          <span className="ml-auto text-xs text-warn animate-pulse">dispatching…</span>
        )}
        {sid && (
          <button
            type="button"
            onClick={() => jumpTo(sid!)}
            className="ml-auto text-xs font-mono px-2 py-0.5 rounded border border-accent/40 hover:bg-accent/15 text-accent"
            title={`jump to subagent transcript ${sid}`}
          >
            ↗ {sid}
          </button>
        )}
      </div>
      <div className="p-3 space-y-2">
        {description && (
          <div>
            <div className="text-xs text-ink-dim mb-1">task</div>
            <div className="text-sm text-ink whitespace-pre-wrap break-words">
              {description}
            </div>
          </div>
        )}
        {summary !== undefined && (
          <div>
            <div className="text-xs text-ink-dim mb-1">summary</div>
            <CollapsibleOutput text={summary} />
          </div>
        )}
      </div>
    </div>
  );
}

function parseBgId(result: string | undefined): string | null {
  if (!result) return null;
  const m = result.match(/\[Background task (bg_[a-zA-Z0-9_-]+) started\]/);
  return m ? m[1] : null;
}

function BackgroundTileBody({ activity, bgId, chatKey }: {
  activity: ChatActivity;
  bgId: string;
  chatKey?: string;
}) {
  const profile = useAuth((s) => s.current());
  // chatKey shape is `${pid}/${sid}` — see Workspace.tsx.
  const [pid, sid] = (chatKey ?? "/").split("/");
  if (!profile || !pid || !sid) {
    return (
      <pre className="text-xs font-mono whitespace-pre-wrap break-all bg-bg-card border border-border rounded p-2">
        {activity.result}
      </pre>
    );
  }
  return (
    <div className="space-y-2">
      <div className="text-xs text-ink-dim">
        Started background task — polling for progress.
      </div>
      <BackgroundTile profile={profile} pid={pid} sid={sid} bgId={bgId} />
    </div>
  );
}

function DiffView({ lines }: { lines: { op: string; text: string }[] }) {
  const shown = lines.slice(0, COLLAPSE_THRESHOLD);
  const hidden = lines.length - shown.length;
  const [expanded, setExpanded] = useState(false);
  const display = expanded ? lines : shown;
  return (
    <div>
      <pre className="text-xs font-mono whitespace-pre-wrap break-all bg-bg-card border border-border rounded p-2 max-h-96 overflow-auto">
        {display.map((l, i) => (
          <div key={i} className={
            l.op === "add" ? "text-ok bg-ok/10" :
            l.op === "del" ? "text-err bg-err/10" : ""}>
            <span className="select-none opacity-60 pr-2">
              {l.op === "add" ? "+" : l.op === "del" ? "−" : " "}
            </span>
            {l.text}
          </div>
        ))}
      </pre>
      {hidden > 0 && (
        <button onClick={() => setExpanded(v => !v)}
          className="text-xs text-accent hover:underline mt-1">
          {expanded ? "Show less" : `Show ${hidden} more lines`}
        </button>
      )}
    </div>
  );
}

function CollapsibleOutput({ text }: { text: string }) {
  const lines = text.split("\n");
  const [expanded, setExpanded] = useState(false);
  if (lines.length <= COLLAPSE_THRESHOLD) {
    return (
      <pre className="text-xs font-mono whitespace-pre-wrap break-all bg-bg-card border border-border rounded p-2 max-h-64 overflow-auto">
        {text}
      </pre>
    );
  }
  const shown = expanded ? text : lines.slice(0, COLLAPSE_THRESHOLD).join("\n");
  return (
    <div>
      <pre className="text-xs font-mono whitespace-pre-wrap break-all bg-bg-card border border-border rounded p-2 max-h-96 overflow-auto">
        {shown}
        {!expanded && (
          <div className="text-ink-dim italic">
            ... ({lines.length - COLLAPSE_THRESHOLD} more lines)
          </div>
        )}
      </pre>
      <button onClick={() => setExpanded(v => !v)}
        className="text-xs text-accent hover:underline mt-1">
        {expanded ? "Show less" : `Show all ${lines.length} lines`}
      </button>
    </div>
  );
}

function summarize(input: Record<string, unknown>): string {
  const vals = Object.values(input ?? {});
  if (vals.length === 0) return "";
  const first = vals[0];
  // Strings/numbers/booleans stringify cleanly, but objects and arrays
  // come back as "[object Object]" — useless in a one-line summary.
  // Pick a compact JSON representation for non-primitives, then truncate.
  const str =
    first !== null &&
    typeof first === "object" &&
    !Array.isArray(first)
      ? JSON.stringify(first)
      : Array.isArray(first)
        ? `[${first.length} item${first.length === 1 ? "" : "s"}]`
        : String(first);
  return str.length > 60 ? str.slice(0, 60) + "…" : str;
}
