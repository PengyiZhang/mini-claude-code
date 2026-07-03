import { useEffect, useRef, useState } from "react";
import { useAuth, useChat } from "../lib/store";
import { runCommandForCard } from "../lib/commands";
import { useTeamActivity } from "../lib/teamActivity";
import type { CardEvent, CardListItem, TeamEvent } from "../lib/types";

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
// Stable empty array reference for the activity selector. Returning a
// fresh `[]` literal from the selector on every render (when pid has no
// activity yet) makes useSyncExternalStore see a "changed" snapshot and
// re-render forever; a module-level constant keeps the snapshot stable.
const EMPTY_ACTIVITY: TeamEvent[] = [];

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

// Truncate to `n` chars, appending … if anything was dropped. Used for
// one-line event summaries so the panel rows stay scannable.
function truncate(s: string, n: number): string {
  if (s.length <= n) return s;
  return s.slice(0, n) + "…";
}

// Render one TeamEvent as a single-line summary. The event shapes come
// from the I.C.1 backend (mini_cc/server/routes/team.py), which spreads
// the original assistant/event payload fields alongside session_id and
// ts. We coerce defensively since the type is permissive.
function summarizeEvent(e: TeamEvent): string {
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

// Format the ts as HH:MM:SS for compactness in the panel. Falls back to
// the raw string on any parse failure (ts comes from backend ISO stamps).
function shortTs(ts: string): string {
  const t = ts.slice(11, 19); // YYYY-MM-DDTHH:MM:SSZ -> HH:MM:SS
  return t || ts;
}

export default function TeammatesPanel({
  pid,
  sid,
}: {
  pid: string;
  sid: string;
}) {
  const profile = useAuth((s) => s.current());
  const runCommand = useChat((s) => s.runCommand);
  const [rows, setRows] = useState<TeamRow[]>([]);
  const [collapsed, setCollapsed] = useState(false);
  const [expanded, setExpanded] = useState<Record<string, boolean>>({});
  const [inboxPeek, setInboxPeek] = useState<Record<string, string[]>>({});
  const abortRef = useRef<AbortController | null>(null);
  // Subscribe to this project's merged activity feed. Selecting just the
  // pid slice keeps re-renders narrow (the store allocates per-pid state
  // on first poll, so default to [] until then).
  const activity = useTeamActivity(
    (s) => s.perProjectActivity[pid] ?? EMPTY_ACTIVITY,
  );

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

  // Poll /team/activity in parallel so the activity feed stays fresh
  // without piling re-renders onto the roster poll. Uses getState() for
  // the action so the component doesn't re-subscribe to the whole store.
  useEffect(() => {
    if (!profile) return;
    const tick = () => {
      useTeamActivity.getState().poll(profile, pid).catch(() => {});
    };
    tick();
    const id = window.setInterval(tick, POLL_MS);
    return () => window.clearInterval(id);
  }, [profile, pid]);

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
            // Keep the full item so we can render disposition badges
            // + ack/ignore buttons. The previous string-only shape was
            // a placeholder from before the history enrichment.
            setInboxPeek((s) => ({ ...s, [r.name]: items as unknown as string[] }));
          })
          .catch(() => {
            setInboxPeek((s) => ({ ...s, [r.name]: ["(failed to peek)"] }));
          });
      }
    }
  }, [rows, expanded, profile, pid, sid, inboxPeek]);

  // Refresh a single teammate's inbox after an ack/ignore click so the
  // badge updates immediately. Re-uses the same fetch path as expand.
  const refreshInbox = (name: string) => {
    if (!profile) return;
    setInboxPeek((s) => ({ ...s, [name]: [] }));
    runCommandForCard(profile, pid, sid, "agents", `inbox ${name}`)
      .then((card) => {
        const items = card
          ? ((card.payload as { items?: CardListItem[] }).items ?? [])
          : [];
        setInboxPeek((s) => ({ ...s, [name]: items as unknown as string[] }));
      })
      .catch(() => {
        setInboxPeek((s) => ({ ...s, [name]: ["(failed to peek)"] }));
      });
  };

  // Always render the panel — empty state shows a placeholder so the
  // user can see the panel exists and discover how to spawn a
  // teammate. Pre-fix, returning null on empty roster made the panel
  // invisible, which looked like a bug ("where did Teammates go?").
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
            {rows.length > 0 && (
              <span className="ml-2 text-ink-faint normal-case">
                {aliveCount} alive · {rows.length - aliveCount} stopped
              </span>
            )}
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
          {rows.length === 0 && (
            <div className="border border-dashed border-border rounded-md p-4 text-center">
              <div className="text-xs text-ink-faint">
                No teammates in this project.
              </div>
              <div className="mt-2 text-xs text-ink-dim">
                Try{" "}
                <span className="font-mono text-ink">
                  /agents spawn &lt;name&gt; &lt;role&gt;
                </span>{" "}
                in chat.
              </div>
            </div>
          )}
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
                  <div className="border-t border-border p-2 space-y-2 bg-bg">
                    <div>
                      <div className="flex items-center justify-between text-xs text-ink-dim uppercase tracking-wide">
                        <span>recent activity</span>
                      </div>
                      {(() => {
                        // Teammate session_id is `teammate-<name>` (see
                        // mini_cc/teams/__init__.py spawn-time loop name).
                        // Take the last 10 events for this teammate and
                        // render newest-first.
                        const events = activity
                          .filter(
                            (e) => e.session_id === `teammate-${r.name}`,
                          )
                          .slice(-10)
                          .reverse();
                        if (events.length === 0) {
                          return (
                            <div className="text-xs text-ink-faint italic">
                              (no recent activity)
                            </div>
                          );
                        }
                        return (
                          <div className="space-y-0.5">
                            {events.map((e, i) => (
                              <div
                                key={`${e.ts}-${i}`}
                                className="text-xs flex gap-2 items-start border-l-2 border-border pl-2"
                              >
                                <span className="text-ink-faint shrink-0 font-mono text-[10px]">
                                  {shortTs(e.ts)}
                                </span>
                                <span className="text-ink whitespace-pre-wrap break-words">
                                  {summarizeEvent(e)}
                                </span>
                              </div>
                            ))}
                          </div>
                        );
                      })()}
                    </div>
                    <div>
                      <div className="flex items-center justify-between text-xs text-ink-dim uppercase tracking-wide">
                        <span>inbox (history)</span>
                      <button
                        onClick={(e) => {
                          e.stopPropagation();
                          refreshInbox(r.name);
                        }}
                        className="text-ink-faint hover:text-ink normal-case"
                        title="refresh inbox"
                      >
                        ↻
                      </button>
                    </div>
                    {(inboxPeek[r.name] ?? []).length === 0 ? (
                      <div className="text-xs text-ink-faint italic">
                        empty
                      </div>
                    ) : (
                      (inboxPeek[r.name] ?? []).map((raw, i) => {
                        // inboxPeek holds either CardListItem-shaped
                        // objects (post-history-enrichment) or fallback
                        // strings (error/legacy). Render accordingly.
                        if (typeof raw === "string") {
                          return (
                            <div
                              key={i}
                              className="text-xs text-ink whitespace-pre-wrap break-words border-l-2 border-border pl-2"
                            >
                              {raw}
                            </div>
                          );
                        }
                        const it = raw as CardListItem;
                        const badge = it.badges[0];
                        const tone = badge?.tone ?? "warn";
                        const badgeColor =
                          tone === "ok"
                            ? "bg-emerald-500/20 text-emerald-700 dark:text-emerald-300"
                            : tone === "muted"
                              ? "bg-bg-hover text-ink-faint"
                              : "bg-amber-500/20 text-amber-700 dark:text-amber-300";
                        return (
                          <div
                            key={it.id ?? i}
                            className="text-xs border-l-2 border-border pl-2 space-y-1"
                          >
                            <div className="flex items-start gap-2">
                              <div className="flex-1 min-w-0">
                                <div className="truncate text-ink">
                                  {it.title}
                                </div>
                                {it.subtitle && (
                                  <div className="text-ink-dim truncate">
                                    {it.subtitle}
                                  </div>
                                )}
                                {it.meta && (
                                  <div className="text-ink-faint text-[10px]">
                                    {it.meta}
                                  </div>
                                )}
                              </div>
                              {badge && (
                                <span
                                  className={
                                    "text-[10px] px-1.5 py-0.5 rounded uppercase font-semibold shrink-0 " +
                                    badgeColor
                                  }
                                >
                                  {badge.text}
                                </span>
                              )}
                            </div>
                            {it.menu.length > 0 && (
                              <div className="flex gap-1">
                                {it.menu.map((act) => (
                                  <button
                                    key={act.label}
                                    onClick={(e) => {
                                      e.stopPropagation();
                                      if (runCommand) {
                                        runCommand(act.command);
                                        // Optimistic refresh — backend
                                        // updates disposition under the
                                        // hood, this swaps the badge.
                                        setTimeout(
                                          () => refreshInbox(r.name),
                                          400,
                                        );
                                      }
                                    }}
                                    className={
                                      "text-[10px] px-1.5 py-0.5 rounded border hover:bg-bg-hover " +
                                      (act.tone === "ok"
                                        ? "border-emerald-500/40 text-emerald-700 dark:text-emerald-300"
                                        : "border-border text-ink-dim")
                                    }
                                  >
                                    {act.label}
                                  </button>
                                ))}
                              </div>
                            )}
                          </div>
                        );
                      })
                    )}
                    </div>
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
