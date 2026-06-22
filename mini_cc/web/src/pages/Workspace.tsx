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
import { useAuth, useChat, useTodos, rawToChatMessages } from "../lib/store";
import type { ChatMessage } from "../lib/store";
import { streamSend } from "../lib/sse";
import { fetchCommands, streamRunCommand } from "../lib/commands";
import type { CommandDef } from "../lib/commands";

type Tab = "chat" | "files" | "sessions";

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
  const startAssistant = useChat((s) => s.startAssistant);
  const appendText = useChat((s) => s.appendText);
  const addActivity = useChat((s) => s.addActivity);
  const setActivityResult = useChat((s) => s.setActivityResult);
  const addNotice = useChat((s) => s.addNotice);
  const finishAssistant = useChat((s) => s.finishAssistant);
  const failAssistant = useChat((s) => s.failAssistant);
  const setStreaming = useChat((s) => s.setStreaming);
  const hydrate = useChat((s) => s.hydrate);

  const abortRef = useRef<AbortController | null>(null);
  const scrollRef = useRef<HTMLDivElement>(null);

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

  useEffect(() => {
    if (scrollRef.current) {
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

  return (
    <div className="min-h-screen flex flex-col">
      <TopBar title={`${pid}`} />
      <div className="flex-1 flex">
        {/* Sidebar */}
        <aside className="w-64 border-r border-border bg-bg-panel p-3 flex flex-col gap-3">
          <div className="flex gap-1 text-sm">
            {(["chat", "files", "sessions"] as Tab[]).map((t) => (
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

          <div className="mt-auto text-xs text-ink-faint">
            tenant <span className="font-mono">{profile.tenantId}</span>
          </div>
        </aside>

        {/* Main */}
        <main className="flex-1 flex flex-col min-w-0">
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

              {chatKey && <TodoPanel chatKey={chatKey} />}

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
                  <textarea
                    rows={2}
                    placeholder={sid ? "send a message…  (type / for commands)" : "create a session first"}
                    disabled={!sid || streaming}
                    value={input}
                    onChange={(e) => setInput(e.target.value)}
                    onKeyDown={(e) => {
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

          {tab === "sessions" && (
            <div className="flex-1 p-6 space-y-3">
              <div className="flex items-center justify-between">
                <h2 className="text-lg font-semibold">Sessions</h2>
                <button
                  onClick={newSession}
                  disabled={busySid}
                  className="px-4 py-1.5 bg-accent text-white rounded"
                >
                  ＋ new
                </button>
              </div>
              <div className="space-y-2">
                {sessions.length === 0 && (
                  <div className="text-sm text-ink-dim">no sessions yet</div>
                )}
                {sessions.map((s) => (
                  <div
                    key={s}
                    className="bg-bg-card border border-border rounded p-3 flex items-center justify-between"
                  >
                    <div className="flex items-center gap-2 font-mono text-sm">
                      <span
                        title={warmSet[s] ? "warm" : "cold"}
                        className={`size-2 rounded-full ${warmSet[s] ? "bg-emerald-500" : "bg-slate-500"}`}
                      />
                      {s}
                    </div>
                    <div className="flex gap-2">
                      <button
                        onClick={() => {
                          setSid(s);
                          setTab("chat");
                        }}
                        className="text-xs px-3 py-1 border border-border rounded hover:border-accent"
                      >
                        open
                      </button>
                      <button
                        onClick={() => removeSession(s)}
                        className="text-xs px-3 py-1 border border-err/50 text-err rounded hover:bg-err/10"
                      >
                        remove
                      </button>
                    </div>
                  </div>
                ))}
              </div>
            </div>
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
