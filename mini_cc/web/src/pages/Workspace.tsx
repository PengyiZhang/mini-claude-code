import { useEffect, useMemo, useRef, useState } from "react";
import { useParams } from "react-router-dom";
import TopBar from "../components/TopBar";
import FileTree from "../components/FileTree";
import FilePreview from "../components/FilePreview";
import MessageBubble from "../components/MessageBubble";
import PermissionPrompt, {
  PermissionPromptData,
  pendingToData,
} from "../components/PermissionPrompt";
import SessionRow from "../components/SessionRow";
import SlashMenu from "../components/SlashMenu";
import TodoPanel from "../components/TodoPanel";
import RunTablePanel from "../components/RunTablePanel";
import TeammatesPanel from "../components/TeammatesPanel";
import MentionPicker, { type MentionCandidate } from "../components/MentionPicker";
import { runCommandForCard } from "../lib/commands";
import type { CardEvent, CardListItem } from "../lib/types";
import {
  ApiError,
  deleteSession,
  downloadZip,
  getSessionMessages,
  getSessionTodos,
  listPendingPermissions,
  listSessionMetas,
  startSession,
} from "../lib/api";
import { useAuth, useChat, useTodos, useSessionNav, rawToChatMessages } from "../lib/store";
import type { ChatMessage } from "../lib/store";
import { streamSend } from "../lib/sse";
import { fetchCommands, streamRunCommand } from "../lib/commands";
import type { CommandDef } from "../lib/commands";

type Tab = "chat" | "files" | "run";

const EMPTY: ChatMessage[] = [];
const EMPTY_PERMS: PermissionPromptData[] = [];

export default function Workspace() {
  const { pid = "" } = useParams();
  const profile = useAuth((s) => s.current())!;
  const [tab, setTab] = useState<Tab>("chat");
  const [sessions, setSessions] = useState<string[]>([]);
  const [warmSet, setWarmSet] = useState<Record<string, boolean>>({});
  const [sid, setSid] = useState<string | null>(null);
  const [busySid, setBusySid] = useState(false);
  const [previewPath, setPreviewPath] = useState<string | null>(null);
  const [treeReload, setTreeReload] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const [input, setInput] = useState("");
  const [pending, setPending] = useState<PermissionPromptData[]>(EMPTY_PERMS);
  const [commands, setCommands] = useState<CommandDef[]>([]);

  const chatKey = sid ? `${pid}::${sid}` : null;
  const chatMessages = useChat((s) => (chatKey ? (s.messages[chatKey] ?? EMPTY) : EMPTY));
  const streaming = useChat((s) => (chatKey ? Boolean(s.streaming[chatKey]) : false));
  const appendUser = useChat((s) => s.appendUser);
  const addTeammateMessage = useChat((s) => s.addTeammateMessage);
  const startAssistant = useChat((s) => s.startAssistant);
  const appendText = useChat((s) => s.appendText);
  const addActivity = useChat((s) => s.addActivity);
  const addCard = useChat((s) => s.addCard);
  const setActivityResult = useChat((s) => s.setActivityResult);
  const addNotice = useChat((s) => s.addNotice);
  const finishAssistant = useChat((s) => s.finishAssistant);
  const failAssistant = useChat((s) => s.failAssistant);
  const setStreaming = useChat((s) => s.setStreaming);
  const hydrate = useChat((s) => s.hydrate);
  const setCommandRunner = useChat((s) => s.setCommandRunner);

  const abortRef = useRef<AbortController | null>(null);
  const scrollRef = useRef<HTMLDivElement>(null);
  // Stable ref to the latest runServerCommand closure so the
  // store-registered command runner (used by CardShell action buttons)
  // can dispatch without prop-drilling. Updated every render.
  const runServerCommandRef = useRef<(name: string, args: string) => void>(
    () => {},
  );

  // ── Slash menu state ─────────────────────────────────────────────
  // Menu shows whenever input looks like "/something" and the user is
  // still typing the command name (no space yet). Once they add a
  // space we treat the command name as fixed and stop filtering.
  const slashOpen = useMemo(() => {
    if (!input.startsWith("/")) return false;
    if (input.includes(" ")) return false;
    return true;
  }, [input]);
  const slashQuery = input.slice(1);  // strip leading "/"
  const slashFiltered = useMemo(() => {
    if (!slashOpen) return [];
    const q = slashQuery.toLowerCase();
    const ranked = commands.filter((c) => {
      if (!q) return true;
      if (c.name.toLowerCase().includes(q)) return true;
      if (c.aliases.some((a) => a.toLowerCase().includes(q))) return true;
      return false;
    });
    return ranked.slice(0, 8);
  }, [commands, slashOpen, slashQuery]);
  const [slashActive, setSlashActive] = useState(0);
  useEffect(() => {
    setSlashActive(0);
  }, [slashQuery]);

  // ── Mention (@) autocomplete state ────────────────────────────────
  // Mirrors the slash menu pattern but for `@<partial>`. Candidates
  // come from a lightweight poll of /agents (every 4s — same cadence
  // as TeammatesPanel) filtered to alive teammates. We only poll while
  // a session is active; the roster is small (<= 10) so cheap.
  const [teamCandidates, setTeamCandidates] = useState<MentionCandidate[]>([]);
  useEffect(() => {
    if (!sid) {
      setTeamCandidates([]);
      return;
    }
    let cancelled = false;
    const POLL_MS = 4000;
    const tick = async () => {
      if (cancelled) return;
      try {
        const card = await runCommandForCard(profile, pid, sid, "agents");
        if (cancelled) return;
        const items = (card?.payload as { items?: CardListItem[] } | undefined)?.items ?? [];
        const alive = items
          .filter((it) =>
            it.badges.some((b) => b.text.toLowerCase() === "alive"),
          )
          .map((it) => ({ name: it.title, role: it.subtitle ?? "" }));
        setTeamCandidates(alive);
      } catch {
        // swallow — next tick retries
      }
    };
    void tick();
    const id = window.setInterval(tick, POLL_MS);
    return () => {
      cancelled = true;
      window.clearInterval(id);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sid, pid, profile.apiKey]);

  // mentionOpen: true when the input ends with `@<partial>` (partial may
  // be empty — that's the bare-`@` case). We match at end so `@bob hi`
  // doesn't keep popping the menu after the user has moved on.
  const mentionMatch = useMemo<null | { start: number; query: string }>(() => {
    if (slashOpen) return null;
    const m = input.match(/(?:^|\s)@([a-zA-Z0-9_一-龥-]*)$/);
    if (!m) return null;
    return { start: m.index! + m[0].length - m[1].length - 1, query: m[1] };
  }, [input, slashOpen]);
  const mentionOpen = mentionMatch !== null && teamCandidates.length > 0;
  const mentionFiltered = useMemo(() => {
    if (!mentionMatch) return [];
    const q = mentionMatch.query.toLowerCase();
    const matches = q
      ? teamCandidates.filter((c) => c.name.toLowerCase().startsWith(q))
      : teamCandidates;
    return matches.slice(0, 8);
  }, [mentionMatch, teamCandidates]);
  const [mentionActive, setMentionActive] = useState(0);
  useEffect(() => {
    setMentionActive(0);
  }, [mentionMatch?.query, mentionMatch?.start]);

  async function refreshSessions() {
    try {
      // Fetch full SessionMeta[] (not just string[]) so we can populate
      // warmSet from the backend's in_memory flag. Without this, every
      // page reload shows all sessions as "cold" even though the server
      // still has them warm — confusing the user about which session
      // they can resume instantly vs. which needs a cold-start.
      const metas = await listSessionMetas(profile, pid);
      const ids = metas.map((m) => m.session_id);
      setSessions(ids);
      setWarmSet((prev) => {
        const next: Record<string, boolean> = {};
        for (const m of metas) {
          // Server-truth wins, but preserve any locally-warmed session
          // that the backend hasn't caught up to yet (race window).
          next[m.session_id] = m.in_memory || Boolean(prev[m.session_id]);
        }
        return next;
      });
      return ids;
    } catch (e) {
      setError(e instanceof ApiError ? e.message : (e as Error).message);
      return [];
    }
  }

  // Poll pending permissions for the active session every few seconds.
  useEffect(() => {
    if (!sid) {
      setPending([]);
      return;
    }
    let cancelled = false;
    async function poll() {
      try {
        const list = await listPendingPermissions(profile, pid, sid!);
        if (cancelled) return;
        setPending(list.map(pendingToData));
      } catch {
        // Ignore — interceptor may not be configured.
      }
    }
    void poll();
    const id = setInterval(poll, 3000);
    return () => {
      cancelled = true;
      clearInterval(id);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sid, pid, profile.apiKey]);

  useEffect(() => {
    refreshSessions().then((list) => {
      if (list.length > 0 && !sid) setSid(list[0]);
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [pid, profile.apiKey]);

  // Hydrate chat history from backend on session activation. Without
  // this, every page reload wipes the conversation even though the
  // backend persists every turn to disk. We only fetch when the local
  // store is empty (hydrate() also re-checks to avoid clobbering an
  // in-flight stream with stale disk state).
  useEffect(() => {
    if (!chatKey || !sid) return;
    const existing = useChat.getState().messages[chatKey];
    if (existing && existing.length > 0) return;
    let cancelled = false;
    getSessionMessages(profile, pid, sid)
      .then((raw) => {
        if (cancelled) return;
        const msgs = rawToChatMessages(raw);
        if (msgs.length > 0) hydrate(chatKey, msgs);
      })
      .catch(() => {
        // Hydration is best-effort — a stale session id or transient
        // 404 shouldn't block the user from starting a fresh chat.
      });
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [chatKey, sid, pid, profile.apiKey]);

  // Hydrate the task board from backend on session activation. Live
  // SSE events will take over once a todo_write fires; this just primes
  // the panel so a refresh doesn't show an empty board.
  useEffect(() => {
    if (!chatKey || !sid) return;
    getSessionTodos(profile, pid, sid)
      .then((todos) => {
        if (todos.length > 0) useTodos.getState().hydrate(chatKey, todos);
      })
      .catch(() => {
        // Best-effort — stale session id shouldn't block the chat.
      });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [chatKey, sid, pid, profile.apiKey]);

  // Auto-scroll to bottom when (a) a new message is appended, or (b)
  // the last message's text grew (streaming). Tool-call expand/collapse
  // mutates `activity.expanded` inside an existing message without
  // changing text length — that case must NOT trigger scroll, otherwise
  // clicking a chevron yanks the user's view to the bottom (debug.6.md #4).
  // Signature = "<count>:<lastTextLen>".
  const prevSigRef = useRef<string>("");
  useEffect(() => {
    const last = chatMessages[chatMessages.length - 1];
    const sig = `${chatMessages.length}:${last ? last.text.length : 0}`;
    const prev = prevSigRef.current;
    prevSigRef.current = sig;
    if (sig !== prev && scrollRef.current) {
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
    }
  }, [chatMessages]);

  // Fetch slash command menu for the active session. The list is the
  // same for every session of the same tenant, but fetching on sid
  // change gives the user a fresh view (e.g. after a new command is
  // registered) and keeps the network path warm. Best-effort —
  // failures just hide the autocomplete menu.
  useEffect(() => {
    if (!sid) {
      setCommands([]);
      return;
    }
    let cancelled = false;
    fetchCommands(profile, pid, sid)
      .then((list) => {
        if (!cancelled) setCommands(list);
      })
      .catch(() => {
        /* ignore — autocomplete just won't show */
      });
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sid, pid, profile.apiKey]);

  async function newSession() {
    setBusySid(true);
    setError(null);
    try {
      const out = await startSession(profile, pid, {});
      setSessions((s) => [...s, out.session_id]);
      setWarmSet((m) => ({ ...m, [out.session_id]: true }));
      setSid(out.session_id);
    } catch (e) {
      setError(e instanceof ApiError ? e.message : (e as Error).message);
    } finally {
      setBusySid(false);
    }
  }

  // Cross-component "jump to session" channel — SubagentDrawer and
  // other deep children call useSessionNav.jumpTo(sid); we apply it
  // here and clear the request.
  const jumpTarget = useSessionNav((s) => s.jumpTarget);
  const consumeJump = useSessionNav((s) => s.consumeJump);
  useEffect(() => {
    if (jumpTarget) {
      setSid(jumpTarget);
      consumeJump();
    }
  }, [jumpTarget, consumeJump]);

  async function removeSession(s: string) {
    if (!confirm(`remove session ${s}?`)) return;
    try {
      await deleteSession(profile, pid, s);
      const list = await refreshSessions();
      if (sid === s) setSid(list[0] ?? null);
    } catch (e) {
      setError(e instanceof ApiError ? e.message : (e as Error).message);
    }
  }

  function abort() {
    // Abort the in-flight SSE stream. streamSend catches AbortError
    // and calls onDone instead of onError, so we must finish the
    // assistant turn here to clear the streaming state — otherwise
    // the send button stays disabled forever.
    abortRef.current?.abort();
    abortRef.current = null;
    // Leave a breadcrumb so the bubble isn't an empty avatar — without
    // this, aborting before any text arrives gives the user no feedback.
    if (chatKey) {
      addNotice(chatKey, "⏹ stopped by user");
      finishAssistant(chatKey);
    }
  }

  async function send() {
    if (!sid || !input.trim() || streaming) return;
    const text = input;
    setInput("");
    appendUser(chatKey!, text);
    startAssistant(chatKey!);
    setStreaming(chatKey!, true);
    setError(null);

    const controller = new AbortController();
    abortRef.current = controller;

    await streamSend(
      {
        url: `${profile.baseUrl}/tenants/${profile.tenantId}/projects/${pid}/sessions/${sid}/send`,
        apiKey: profile.apiKey,
        body: { user_input: text },
        signal: controller.signal,
        // B8: auto-resume from the per-session event log if the
        // connection drops mid-stream. Server-side resume-only path
        // replays missed events without triggering a duplicate run.
        reconnect: true,
      },
      {
        onEvent: (ev) => {
          switch (ev.type) {
            case "text":
              appendText(chatKey!, ev.text);
              break;
            case "tool_use":
              addActivity(chatKey!, {
                kind: "tool_use",
                id: ev.id,
                name: ev.name,
                input: ev.input,
                expanded: false,
              });
              break;
            case "tool_result":
              setActivityResult(chatKey!, ev.tool_use_id, ev.content);
              break;
            case "permission_request":
              setPending((p) =>
                p.some((x) => x.request_id === ev.request_id)
                  ? p
                  : [
                      ...p,
                      {
                        request_id: ev.request_id,
                        tool_name: ev.tool_name,
                        tool_input: ev.tool_input,
                        ttl_seconds: ev.ttl_seconds,
                      },
                    ],
              );
              break;
            case "permission_resolved":
              setPending((p) => p.filter((x) => x.request_id !== ev.request_id));
              break;
            case "session_warm":
              setWarmSet((m) => ({ ...m, [ev.session_id]: true }));
              break;
            case "session_resumed":
              // Backend warmed a different session via /resume <id>. Rotate
              // the active session so the chat pane follows. The existing
              // hydrate useEffect (keyed on sid) pulls the new session's
              // history from disk.
              if (ev.session_id && ev.session_id !== sid) {
                setSid(ev.session_id);
              }
              break;
            case "todos_updated":
              if (chatKey) useTodos.getState().setTodos(chatKey, ev.todos);
              break;
            case "done":
              finishAssistant(chatKey!);
              break;
            case "error":
              failAssistant(chatKey!, ev.message);
              break;
            case "max_tokens_escalation":
              addNotice(chatKey!, `max_tokens escalated to ${ev.max_tokens}`);
              break;
            case "cron_fired":
              addNotice(chatKey!, `cron fired: ${ev.prompt}`);
              break;
            case "background_notification":
              addNotice(chatKey!, "background task reported back");
              break;
            case "teammate_message":
              // debug.8 Task A: live teammate→lead delivery via the
              // spawner's lead side-channel. Render as its own bubble
              // with a distinct avatar so the user can tell at a
              // glance who's talking.
              if (ev.from && ev.content) {
                addTeammateMessage(chatKey!, ev.from, ev.content);
              }
              break;
          }
        },
        onError: (e) => {
          failAssistant(chatKey!, e.message);
          setError(e.message);
        },
      },
    );
    abortRef.current = null;
  }

  async function download() {
    try {
      const blob = await downloadZip(profile, pid);
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `${pid}.zip`;
      a.click();
      URL.revokeObjectURL(url);
    } catch (e) {
      setError(e instanceof ApiError ? e.message : (e as Error).message);
    }
  }

  // Run a server-scoped slash command. Output flows through the same
  // chat store as a normal turn so the user sees the result inline.
  async function runServerCommand(name: string, args: string) {
    if (!sid || !chatKey || streaming) return;
    appendUser(chatKey, `/${name}${args ? ` ${args}` : ""}`);
    startAssistant(chatKey);
    setStreaming(chatKey, true);
    setError(null);

    const controller = new AbortController();
    abortRef.current = controller;

    await streamRunCommand(
      profile,
      pid,
      sid,
      name,
      {
        onEvent: (ev) => {
          switch (ev.type) {
            case "text":
              appendText(chatKey!, ev.text);
              break;
            case "card":
              addCard(chatKey!, ev);
              break;
            case "done":
              finishAssistant(chatKey!);
              break;
            case "error":
              failAssistant(chatKey!, ev.message);
              break;
            default:
              // tool_use / tool_result / permission_* etc. aren't
              // produced by built-in commands; keep render path simple.
              break;
          }
        },
        onError: (e) => {
          failAssistant(chatKey!, e.message);
          setError(e.message);
        },
      },
      args,
      controller.signal,
    );
    abortRef.current = null;
  }
  runServerCommandRef.current = runServerCommand;

  // Register a stable command runner with the chat store so any deep
  // component (CardShell action buttons, future inline forms, …) can
  // trigger a slash command without prop-drilling. The runner parses
  // "/name args" exactly like a manually-typed command and forwards to
  // the latest runServerCommand closure via a ref.
  useEffect(() => {
    setCommandRunner((cmd) => {
      const stripped = cmd.replace(/^\//, "");
      const sep = stripped.search(/\s/);
      const name = sep === -1 ? stripped : stripped.slice(0, sep);
      const args = sep === -1 ? "" : stripped.slice(sep + 1);
      runServerCommandRef.current(name, args);
    });
    return () => setCommandRunner(null);
  }, [setCommandRunner]);

  // Dispatch a slash command picked from the menu. Server-scoped →
  // POST to /commands/{name}. Client-scoped commands are not yet
  // wired (registry only has server commands today); route anything
  // unknown through the server path and let it 404 if it doesn't
  // exist.
  function pickCommand(cmd: CommandDef) {
    setInput("");
    void runServerCommand(cmd.name, "");
  }

  function autocompleteCommand(cmd: CommandDef) {
    setInput(`/${cmd.name} `);
  }

  function pickMention(name: string) {
    if (!mentionMatch) return;
    const before = input.slice(0, mentionMatch.start);
    const after = input.slice(mentionMatch.start + mentionMatch.query.length + 1);
    // Insert `@name ` so the user can keep typing the message body.
    setInput(`${before}@${name} ${after}`);
  }

  return (
    <div className="h-screen overflow-hidden flex flex-col">
      <TopBar title={`${pid}`} />
      <div className="flex-1 flex min-h-0">
        {/* Sidebar */}
        <aside className="w-64 border-r border-border bg-bg-panel p-3 flex flex-col gap-3 overflow-y-auto shrink-0">
          <div className="flex gap-1 text-sm">
            {(["chat", "files", "run"] as Tab[]).map((t) => (
              <button
                key={t}
                onClick={() => setTab(t)}
                className={`px-3 py-1.5 rounded ${
                  tab === t ? "bg-accent text-white" : "hover:bg-bg-hover"
                }`}
              >
                {t}
              </button>
            ))}
          </div>

          {tab === "chat" && (
            <div className="space-y-2">
              <div className="text-xs text-ink-dim uppercase tracking-wide">sessions</div>
              <button
                onClick={newSession}
                disabled={busySid}
                className="w-full text-sm px-3 py-1.5 rounded border border-border hover:border-accent disabled:opacity-50"
              >
                ＋ new session
              </button>
              <div className="space-y-1">
                {sessions.map((s) => (
                  <SessionRow
                    key={s}
                    sid={s}
                    profile={profile}
                    pid={pid}
                    active={sid === s}
                    inMemory={warmSet[s] ?? false}
                    onClick={() => setSid(s)}
                    onRemove={() => removeSession(s)}
                  />
                ))}
              </div>
            </div>
          )}

          {tab === "files" && (
            <div className="space-y-2">
              <div className="text-xs text-ink-dim">
                Workspace files live in the tree. Use the <span className="font-mono">＋</span> button at the top of the tree to upload, and the <span className="font-mono">⬇</span> button to download. Right-click any folder for per-folder actions.
              </div>
            </div>
          )}

          {tab === "run" && (
            <RunTablePanel profile={profile} pid={pid} sid={sid} />
          )}

          <div className="mt-auto text-xs text-ink-faint">
            tenant <span className="font-mono">{profile.tenantId}</span>
          </div>
        </aside>

        {/* Main + right-side TeammatesPanel (debug.8 Task A). Panel only
            renders when there's at least one teammate in the project, so
            for teammate-less sessions the chat pane owns the full width. */}
        <main className="flex-1 flex min-h-0 min-w-0">
          <div className="flex-1 flex flex-col min-w-0">
          {error && (
            <div className="text-sm text-err bg-err/10 border-b border-err/40 px-4 py-2">
              {error}
            </div>
          )}

          {tab === "chat" && (
            <>
              {/* Pending permission prompts */}
              {sid && pending.length > 0 && (
                <div className="space-y-2 p-3 border-b border-amber-500/30 bg-amber-500/5">
                  {pending.map((p) => (
                    <PermissionPrompt
                      key={p.request_id}
                      profile={profile}
                      pid={pid}
                      sid={sid}
                      data={p}
                      onResolved={(reqId) =>
                        setPending((cur) => cur.filter((x) => x.request_id !== reqId))
                      }
                    />
                  ))}
                </div>
              )}

              <div ref={scrollRef} className="flex-1 overflow-auto p-6 space-y-4">
                {!sid ? (
                  <div className="text-sm text-ink-dim">no session — click “new session” to begin</div>
                ) : chatMessages.length === 0 ? (
                  <div className="text-sm text-ink-dim">
                    ask mini_cc anything about project <span className="font-mono">{pid}</span>
                  </div>
                ) : (
                  chatMessages.map((m, i) => (
                    <MessageBubbleWithKey key={i} msg={m} chatKey={chatKey!} />
                  ))
                )}
              </div>

              {/* Task board pinned just above the input so it stays visible
                  while scrolling and survives refresh (hydrated from disk).
                  Returns null when empty, so it costs no vertical space. */}
              {chatKey && <TodoPanel chatKey={chatKey} />}

              <div className="border-t border-border p-4 bg-bg-panel">
                <div className="flex gap-2 relative">
                  {slashOpen && commands.length > 0 && (
                    <SlashMenu
                      commands={slashFiltered}
                      active={Math.min(slashActive, Math.max(slashFiltered.length - 1, 0))}
                      onHover={(i) => setSlashActive(i)}
                      onPick={(c) => pickCommand(c)}
                    />
                  )}
                  {mentionOpen && mentionFiltered.length > 0 && (
                    <MentionPicker
                      query={mentionMatch?.query ?? ""}
                      candidates={mentionFiltered}
                      activeIndex={Math.min(
                        mentionActive,
                        Math.max(mentionFiltered.length - 1, 0),
                      )}
                      onPick={(name) => pickMention(name)}
                      onClose={() => {
                        // Clear the @-token to dismiss the picker without
                        // disturbing the rest of the input.
                        if (mentionMatch) {
                          const before = input.slice(0, mentionMatch.start);
                          const after = input.slice(
                            mentionMatch.start + mentionMatch.query.length + 1,
                          );
                          setInput(`${before}${after}`);
                        }
                      }}
                    />
                  )}
                  <textarea
                    rows={2}
                    placeholder={sid ? "send a message…  (type / for commands)" : "create a session first"}
                    disabled={!sid || streaming}
                    value={input}
                    onChange={(e) => setInput(e.target.value)}
                    onKeyDown={(e) => {
                      if (mentionOpen && mentionFiltered.length > 0) {
                        const n = mentionFiltered.length;
                        if (e.key === "ArrowDown") {
                          e.preventDefault();
                          setMentionActive((i) => (i + 1) % n);
                          return;
                        }
                        if (e.key === "ArrowUp") {
                          e.preventDefault();
                          setMentionActive((i) => (i - 1 + n) % n);
                          return;
                        }
                        if (e.key === "Enter" && !e.shiftKey) {
                          e.preventDefault();
                          const pick =
                            mentionFiltered[Math.min(mentionActive, n - 1)];
                          if (pick) pickMention(pick.name);
                          return;
                        }
                        if (e.key === "Tab") {
                          e.preventDefault();
                          const pick =
                            mentionFiltered[Math.min(mentionActive, n - 1)];
                          if (pick) pickMention(pick.name);
                          return;
                        }
                        if (e.key === "Escape") {
                          e.preventDefault();
                          // Strip the @-token to dismiss the picker.
                          if (mentionMatch) {
                            const before = input.slice(0, mentionMatch.start);
                            const after = input.slice(
                              mentionMatch.start + mentionMatch.query.length + 1,
                            );
                            setInput(`${before}${after}`);
                          }
                          return;
                        }
                      }
                      if (slashOpen) {
                        if (e.key === "ArrowDown") {
                          e.preventDefault();
                          if (slashFiltered.length > 0) {
                            setSlashActive((i) => (i + 1) % slashFiltered.length);
                          }
                          return;
                        }
                        if (e.key === "ArrowUp") {
                          e.preventDefault();
                          if (slashFiltered.length > 0) {
                            setSlashActive((i) => (i - 1 + slashFiltered.length) % slashFiltered.length);
                          }
                          return;
                        }
                        if (e.key === "Enter" && !e.shiftKey) {
                          e.preventDefault();
                          const pick = slashFiltered[Math.min(slashActive, slashFiltered.length - 1)];
                          if (pick) pickCommand(pick);
                          return;
                        }
                        if (e.key === "Tab") {
                          e.preventDefault();
                          const pick = slashFiltered[Math.min(slashActive, slashFiltered.length - 1)];
                          if (pick) autocompleteCommand(pick);
                          return;
                        }
                        if (e.key === "Escape") {
                          e.preventDefault();
                          setInput("");
                          return;
                        }
                      }
                      if (e.key === "Enter" && !e.shiftKey) {
                        e.preventDefault();
                        const trimmed = input.trim();
                        if (trimmed.startsWith("/")) {
                          // /command [args] — dispatch even after the
                          // menu closes (when args are typed in).
                          const parts = trimmed.slice(1).split(/\s+/);
                          const name = parts[0];
                          const args = parts.slice(1).join(" ");
                          if (name) {
                            setInput("");
                            void runServerCommand(name, args);
                            return;
                          }
                        }
                        send();
                      }
                    }}
                    className="flex-1 bg-bg border border-border rounded px-3 py-2 text-sm outline-none focus:border-accent resize-none disabled:opacity-50"
                  />
                  <button
                    onClick={streaming ? abort : send}
                    disabled={!sid || (!streaming && !input.trim())}
                    className={`px-4 text-white rounded font-medium disabled:opacity-50 ${
                      streaming
                        ? "bg-err hover:bg-err/90"
                        : "bg-accent hover:bg-accent-hover"
                    }`}
                  >
                    {streaming ? "■ stop" : "send"}
                  </button>
                </div>
              </div>
            </>
          )}

          {tab === "files" && (
            <div className="flex-1 grid grid-cols-1 lg:grid-cols-2 gap-0 min-h-0">
              <div className="border-r border-border overflow-auto p-3">
                <FileTree
                  pid={pid}
                  onPickFile={(p) => setPreviewPath(p)}
                  reloadKey={treeReload}
                  onDownloadZip={download}
                />
              </div>
              <div className="overflow-auto p-4">
                {previewPath ? (
                  <FilePreview pid={pid} path={previewPath} />
                ) : (
                  <div className="text-sm text-ink-dim">
                    select a file to preview, or use the ＋ button above the tree to add files.
                  </div>
                )}
              </div>
            </div>
          )}
          </div>
          {tab === "chat" && sid && (
            <TeammatesPanel pid={pid} sid={sid} />
          )}
        </main>
      </div>
    </div>
  );
}

function MessageBubbleWithKey({ msg, chatKey }: { msg: ReturnType<typeof useChat.getState>["messages"][string][number]; chatKey: string }) {
  // inject the chat key into the message so Activity can toggle it.
  const augmented = { ...msg, __key: chatKey } as typeof msg & { __key: string };
  return <MessageBubble msg={augmented} />;
}
