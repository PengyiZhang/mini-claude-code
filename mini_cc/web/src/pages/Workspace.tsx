import { useEffect, useRef, useState } from "react";
import { useParams } from "react-router-dom";
import TopBar from "../components/TopBar";
import FileTree from "../components/FileTree";
import FilePreview from "../components/FilePreview";
import MessageBubble from "../components/MessageBubble";
import { ApiError, deleteSession, downloadZip, listSessions, startSession } from "../lib/api";
import { useAuth, useChat } from "../lib/store";
import type { ChatMessage } from "../lib/store";
import { streamSend } from "../lib/sse";

type Tab = "chat" | "files" | "sessions";

const EMPTY: ChatMessage[] = [];

export default function Workspace() {
  const { pid = "" } = useParams();
  const profile = useAuth((s) => s.current())!;
  const [tab, setTab] = useState<Tab>("chat");
  const [sessions, setSessions] = useState<string[]>([]);
  const [sid, setSid] = useState<string | null>(null);
  const [busySid, setBusySid] = useState(false);
  const [previewPath, setPreviewPath] = useState<string | null>(null);
  const [treeReload, setTreeReload] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const [input, setInput] = useState("");

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

  const abortRef = useRef<AbortController | null>(null);
  const scrollRef = useRef<HTMLDivElement>(null);

  async function refreshSessions() {
    try {
      const list = await listSessions(profile, pid);
      setSessions(list);
      return list;
    } catch (e) {
      setError(e instanceof ApiError ? e.message : (e as Error).message);
      return [];
    }
  }

  useEffect(() => {
    refreshSessions().then((list) => {
      if (list.length > 0 && !sid) setSid(list[0]);
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [pid, profile.apiKey]);

  useEffect(() => {
    if (scrollRef.current) {
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
    }
  }, [chatMessages]);

  async function newSession() {
    setBusySid(true);
    setError(null);
    try {
      const out = await startSession(profile, pid, {});
      setSessions((s) => [...s, out.session_id]);
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
                  <div
                    key={s}
                    className={`text-sm font-mono px-2 py-1 rounded cursor-pointer flex justify-between items-center group ${
                      sid === s ? "bg-bg-hover text-ink" : "text-ink-dim hover:bg-bg-hover"
                    }`}
                    onClick={() => setSid(s)}
                  >
                    <span className="truncate flex-1">{s}</span>
                    <button
                      onClick={(e) => {
                        e.stopPropagation();
                        removeSession(s);
                      }}
                      className="opacity-0 group-hover:opacity-100 text-err/70 hover:text-err text-xs"
                    >
                      ×
                    </button>
                  </div>
                ))}
              </div>
            </div>
          )}

          {tab === "files" && (
            <div className="space-y-2">
              <button
                onClick={download}
                className="w-full text-sm px-3 py-1.5 rounded border border-border hover:border-accent"
              >
                ⬇ Download ZIP
              </button>
              <div className="text-xs text-ink-dim uppercase tracking-wide">workspace</div>
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
                <div className="flex gap-2">
                  <textarea
                    rows={2}
                    placeholder={sid ? "send a message…" : "create a session first"}
                    disabled={!sid || streaming}
                    value={input}
                    onChange={(e) => setInput(e.target.value)}
                    onKeyDown={(e) => {
                      if (e.key === "Enter" && !e.shiftKey) {
                        e.preventDefault();
                        send();
                      }
                    }}
                    className="flex-1 bg-bg border border-border rounded px-3 py-2 text-sm outline-none focus:border-accent resize-none disabled:opacity-50"
                  />
                  <button
                    onClick={send}
                    disabled={!sid || streaming || !input.trim()}
                    className="px-4 bg-accent hover:bg-accent-hover disabled:opacity-50 text-white rounded font-medium"
                  >
                    {streaming ? "…" : "send"}
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
                />
              </div>
              <div className="overflow-auto p-4">
                {previewPath ? (
                  <FilePreview pid={pid} path={previewPath} />
                ) : (
                  <div className="text-sm text-ink-dim">
                    select a file to preview, or right-click any folder to upload files or folders.
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
                    <div className="font-mono text-sm">{s}</div>
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
