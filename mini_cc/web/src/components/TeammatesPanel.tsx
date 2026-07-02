import { useEffect, useRef, useState } from "react";
import { useAuth } from "../lib/store";
import { runCommandForCard } from "../lib/commands";
import type { CardEvent, CardListItem } from "../lib/types";

/**
 * TeammatesPanel (debug.8 Task A)
 *
 * Right-side panel that lists every teammate in the project with live
 * status, role, age, and the last few messages they've exchanged with
 * the lead. Renders as a collapsible column so the chat pane stays
 * usable when there are no teammates.
 *
 * Data source: polls `/agents` every 4s via `runCommandForCard`. The
 * roster card carries items with title=name, subtitle=role/age,
 * badges=[ALIVE|STOPPED], and inbox count meta. We render those plus
 * a per-teammate collapsible detail that fetches the latest inbox
 * peek on demand (lazy — only fetch when the user expands the row).
 *
 * The panel auto-shows whenever there's at least one teammate (alive
 * or recently stopped). When the roster is empty, it stays collapsed
 * to a thin tab on the right edge so the chat pane owns the full width.
 */

const POLL_MS = 4000;

interface TeamRow {
  name: string;
  role: string;
  alive: boolean;
  meta: string;
}

function parseRoster(card: CardEvent | null): TeamRow[] {
  if (!card) return [];
  const items = (card.payload as { items?: CardListItem[] }).items ?? [];
  return items.map((it) => {
    const alive = it.badges.some((b) => b.text.toLowerCase() === "alive");
    return {
      name: it.title,
      role: it.subtitle ?? "",
      alive,
      meta: it.meta ?? "",
    };
  });
}

export default function TeammatesPanel({
  pid,
  sid,
}: {
  pid: string;
  sid: string;
}) {
  const profile = useAuth((s) => s.current());
  const [rows, setRows] = useState<TeamRow[]>([]);
  const [collapsed, setCollapsed] = useState(false);
  const [expanded, setExpanded] = useState<Record<string, boolean>>({});
  const [inboxPeek, setInboxPeek] = useState<Record<string, string[]>>({});
  const abortRef = useRef<AbortController | null>(null);

  // Poll /agents for the live roster.
  useEffect(() => {
    if (!profile) return;
    let cancelled = false;
    const tick = async () => {
      if (cancelled) return;
      try {
        const fresh = await runCommandForCard(profile, pid, sid, "agents");
        if (!cancelled) setRows(parseRoster(fresh));
      } catch {
        // swallow — next tick retries
      }
    };
    tick();
    const id = window.setInterval(tick, POLL_MS);
    return () => {
      cancelled = true;
      window.clearInterval(id);
      abortRef.current?.abort();
    };
  }, [profile, pid, sid]);

  // Peek inbox for a teammate on expand.
  useEffect(() => {
    if (!profile) return;
    for (const r of rows) {
      if (expanded[r.name] && inboxPeek[r.name] === undefined) {
        // mark pending to avoid refetch loop
        setInboxPeek((s) => ({ ...s, [r.name]: [] }));
        runCommandForCard(profile, pid, sid, "agents", `inbox ${r.name}`)
          .then((card) => {
            const items = card
              ? ((card.payload as { items?: CardListItem[] }).items ?? [])
              : [];
            const lines = items.map((it) =>
              `${it.title}${it.subtitle ? ` — ${it.subtitle}` : ""}`);
            setInboxPeek((s) => ({ ...s, [r.name]: lines }));
          })
          .catch(() => {
            setInboxPeek((s) => ({ ...s, [r.name]: ["(failed to peek)"] }));
          });
      }
    }
  }, [rows, expanded, profile, pid, sid, inboxPeek]);

  // No teammates → collapse to a thin edge tab so the chat pane owns the space.
  if (rows.length === 0) {
    return null;
  }

  const aliveCount = rows.filter((r) => r.alive).length;

  return (
    <aside
      className={
        "border-l border-border bg-bg-panel flex flex-col min-h-0 shrink-0 transition-all " +
        (collapsed ? "w-10" : "w-72")
      }
    >
      <div className="flex items-center justify-between px-3 py-2 border-b border-border">
        {!collapsed && (
          <div className="text-xs uppercase tracking-wide text-ink-dim">
            Teammates
            <span className="ml-2 text-ink-faint normal-case">
              {aliveCount} alive · {rows.length - aliveCount} stopped
            </span>
          </div>
        )}
        <button
          onClick={() => setCollapsed((v) => !v)}
          className="text-xs text-ink-dim hover:text-ink px-1.5 py-0.5 rounded hover:bg-bg-hover"
          aria-label={collapsed ? "Expand teammates panel" : "Collapse teammates panel"}
          title={collapsed ? "Expand" : "Collapse"}
        >
          {collapsed ? "◀" : "▶"}
        </button>
      </div>
      {!collapsed && (
        <div className="flex-1 overflow-auto p-2 space-y-2">
          {rows.map((r) => {
            const isOpen = expanded[r.name] ?? false;
            return (
              <div
                key={r.name}
                className={
                  "border rounded-md text-sm " +
                  (r.alive
                    ? "border-emerald-500/40 bg-emerald-500/5"
                    : "border-border bg-bg-card opacity-70")
                }
              >
                <button
                  onClick={() =>
                    setExpanded((s) => ({ ...s, [r.name]: !isOpen }))
                  }
                  className="w-full flex items-center gap-2 px-2 py-1.5 text-left hover:bg-bg-hover"
                >
                  <span className="text-xs text-ink-dim">{isOpen ? "▼" : "▶"}</span>
                  <span className="size-6 rounded bg-gradient-to-br from-emerald-500 to-teal-400 text-white text-xs flex items-center justify-center font-semibold uppercase shrink-0">
                    {r.name.slice(0, 1)}
                  </span>
                  <div className="flex-1 min-w-0">
                    <div className="truncate">
                      <span className="font-medium text-ink">@{r.name}</span>
                      <span className="ml-2 text-xs text-ink-dim truncate">{r.role}</span>
                    </div>
                    {r.meta && (
                      <div className="text-xs text-ink-faint truncate">{r.meta}</div>
                    )}
                  </div>
                  <span
                    className={
                      "text-[10px] px-1.5 py-0.5 rounded uppercase font-semibold " +
                      (r.alive
                        ? "bg-emerald-500/20 text-emerald-700 dark:text-emerald-300"
                        : "bg-bg-hover text-ink-dim")
                    }
                  >
                    {r.alive ? "alive" : "stopped"}
                  </span>
                </button>
                {isOpen && (
                  <div className="border-t border-border p-2 space-y-1 bg-bg">
                    <div className="text-xs text-ink-dim uppercase tracking-wide">
                      inbox (peek)
                    </div>
                    {(inboxPeek[r.name] ?? []).length === 0 ? (
                      <div className="text-xs text-ink-faint italic">
                        empty
                      </div>
                    ) : (
                      (inboxPeek[r.name] ?? []).map((line, i) => (
                        <div
                          key={i}
                          className="text-xs text-ink whitespace-pre-wrap break-words border-l-2 border-border pl-2"
                        >
                          {line}
                        </div>
                      ))
                    )}
                  </div>
                )}
              </div>
            );
          })}
          <div className="text-xs text-ink-faint pt-1">
            tip: use{" "}
            <span className="font-mono">/agents</span> in chat to manage
            teammates
          </div>
        </div>
      )}
    </aside>
  );
}
