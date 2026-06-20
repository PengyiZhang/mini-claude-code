import type { ChatActivity, ChatMessage } from "../lib/store";

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
          <div className={`whitespace-pre-wrap break-words text-ink ${msg.streaming ? "streaming-cursor" : ""}`}>
            {msg.text}
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
  return (
    <div className="border border-border bg-bg-card rounded-md text-sm overflow-hidden">
      <button
        onClick={() => key && toggle(key, activity.id)}
        className="w-full flex items-center gap-2 px-3 py-2 hover:bg-bg-hover text-left"
      >
        <span className="text-xs text-ink-dim">{activity.expanded ? "▼" : "▶"}</span>
        <span className="text-accent">🔧 {activity.name}</span>
        <span className="text-ink-dim text-xs truncate">{summarize(activity.input)}</span>
        {activity.result === undefined ? (
          <span className="ml-auto text-xs text-warn animate-pulse">running…</span>
        ) : (
          <span className="ml-auto text-xs text-ok">✓ done</span>
        )}
      </button>
      {activity.expanded && (
        <div className="border-t border-border p-3 space-y-2 bg-bg">
          <div>
            <div className="text-xs text-ink-dim mb-1">input</div>
            <pre className="text-xs font-mono whitespace-pre-wrap break-all bg-bg-card border border-border rounded p-2">
              {JSON.stringify(activity.input, null, 2)}
            </pre>
          </div>
          {activity.result !== undefined && (
            <div>
              <div className="text-xs text-ink-dim mb-1">result</div>
              <pre className="text-xs font-mono whitespace-pre-wrap break-all bg-bg-card border border-border rounded p-2 max-h-64 overflow-auto">
                {activity.result}
              </pre>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function summarize(input: Record<string, unknown>): string {
  const vals = Object.values(input ?? {});
  if (vals.length === 0) return "";
  const first = String(vals[0]);
  return first.length > 60 ? first.slice(0, 60) + "…" : first;
}
