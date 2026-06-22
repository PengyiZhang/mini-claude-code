import { create } from "zustand";
import type { TenantProfile, TodoItem } from "./types";

const LS_KEY = "mini_cc.tenants.v1";
const LS_ACTIVE = "mini_cc.tenants.active";

interface TenantState {
  profiles: TenantProfile[];
  activeApiKey: string | null;
  load: () => void;
  add: (p: TenantProfile) => void;
  remove: (apiKey: string) => void;
  setActive: (apiKey: string) => void;
  current: () => TenantProfile | null;
}

function loadProfiles(): TenantProfile[] {
  try {
    const raw = localStorage.getItem(LS_KEY);
    return raw ? (JSON.parse(raw) as TenantProfile[]) : [];
  } catch {
    return [];
  }
}

function saveProfiles(p: TenantProfile[]) {
  localStorage.setItem(LS_KEY, JSON.stringify(p));
}

function initialProfiles(): TenantProfile[] {
  if (typeof window === "undefined") return [];
  return loadProfiles();
}

function initialActive(profiles: TenantProfile[]): string | null {
  if (typeof window === "undefined") return null;
  const active = localStorage.getItem(LS_ACTIVE);
  return profiles.some((p) => p.apiKey === active) ? active : null;
}

const _initialProfiles = initialProfiles();

export const useAuth = create<TenantState>((set, get) => ({
  profiles: _initialProfiles,
  activeApiKey: initialActive(_initialProfiles),
  load: () => {
    const profiles = loadProfiles();
    const active = localStorage.getItem(LS_ACTIVE);
    set({
      profiles,
      activeApiKey: profiles.some((p) => p.apiKey === active) ? active : null,
    });
  },
  add: (p) =>
    set((s) => {
      const others = s.profiles.filter((x) => x.apiKey !== p.apiKey);
      const next = [...others, p];
      saveProfiles(next);
      localStorage.setItem(LS_ACTIVE, p.apiKey);
      return { profiles: next, activeApiKey: p.apiKey };
    }),
  remove: (apiKey) =>
    set((s) => {
      const next = s.profiles.filter((x) => x.apiKey !== apiKey);
      saveProfiles(next);
      if (s.activeApiKey === apiKey) {
        localStorage.removeItem(LS_ACTIVE);
        return { profiles: next, activeApiKey: null };
      }
      return { profiles: next };
    }),
  setActive: (apiKey) => {
    localStorage.setItem(LS_ACTIVE, apiKey);
    set({ activeApiKey: apiKey });
  },
  current: () => {
    const { profiles, activeApiKey } = get();
    return profiles.find((p) => p.apiKey === activeApiKey) ?? null;
  },
}));

// Chat state: per-session conversation log + transient streaming status
export interface ChatActivity {
  kind: "tool_use";
  id: string;
  name: string;
  input: Record<string, unknown>;
  result?: string;
  expanded: boolean;
}

export interface ChatMessage {
  role: "user" | "assistant";
  text: string;
  streaming?: boolean;
  activities?: ChatActivity[];
  error?: string;
  notices?: string[];
}

interface ChatState {
  // key: `${pid}::${sid}`
  messages: Record<string, ChatMessage[]>;
  streaming: Record<string, boolean>;
  appendUser: (key: string, text: string) => void;
  startAssistant: (key: string) => void;
  appendText: (key: string, text: string) => void;
  addActivity: (key: string, act: ChatActivity) => void;
  setActivityResult: (key: string, toolUseId: string, content: string) => void;
  toggleActivity: (key: string, toolUseId: string) => void;
  addNotice: (key: string, text: string) => void;
  finishAssistant: (key: string) => void;
  failAssistant: (key: string, msg: string) => void;
  setStreaming: (key: string, on: boolean) => void;
  clear: (key: string) => void;
  hydrate: (key: string, msgs: ChatMessage[]) => void;
}

function lastAssistant(list: ChatMessage[]): ChatMessage | undefined {
  for (let i = list.length - 1; i >= 0; i--) {
    if (list[i].role === "assistant") return list[i];
  }
  return undefined;
}

export const useChat = create<ChatState>((set) => ({
  messages: {},
  streaming: {},
  appendUser: (key, text) =>
    set((s) => ({
      messages: { ...s.messages, [key]: [...(s.messages[key] ?? []), { role: "user", text }] },
    })),
  startAssistant: (key) =>
    set((s) => ({
      messages: {
        ...s.messages,
        [key]: [...(s.messages[key] ?? []), { role: "assistant", text: "", streaming: true, activities: [], notices: [] }],
      },
    })),
  appendText: (key, text) =>
    set((s) => {
      const list = [...(s.messages[key] ?? [])];
      const last = lastAssistant(list);
      if (last && last.streaming) {
        last.text += text;
      }
      return { messages: { ...s.messages, [key]: list } };
    }),
  addActivity: (key, act) =>
    set((s) => {
      const list = [...(s.messages[key] ?? [])];
      const last = lastAssistant(list);
      if (last) {
        last.activities = [...(last.activities ?? []), act];
      }
      return { messages: { ...s.messages, [key]: list } };
    }),
  setActivityResult: (key, toolUseId, content) =>
    set((s) => {
      const list = [...(s.messages[key] ?? [])];
      const last = lastAssistant(list);
      if (last?.activities) {
        const act = last.activities.find((a) => a.id === toolUseId);
        if (act) act.result = content;
      }
      return { messages: { ...s.messages, [key]: list } };
    }),
  toggleActivity: (key, toolUseId) =>
    set((s) => {
      const list = [...(s.messages[key] ?? [])];
      const last = lastAssistant(list);
      if (last?.activities) {
        const act = last.activities.find((a) => a.id === toolUseId);
        if (act) act.expanded = !act.expanded;
      }
      return { messages: { ...s.messages, [key]: list } };
    }),
  addNotice: (key, text) =>
    set((s) => {
      const list = [...(s.messages[key] ?? [])];
      const last = lastAssistant(list);
      if (last) {
        last.notices = [...(last.notices ?? []), text];
      }
      return { messages: { ...s.messages, [key]: list } };
    }),
  finishAssistant: (key) =>
    set((s) => {
      const list = [...(s.messages[key] ?? [])];
      const last = lastAssistant(list);
      if (last) last.streaming = false;
      return { messages: { ...s.messages, [key]: list }, streaming: { ...s.streaming, [key]: false } };
    }),
  failAssistant: (key, msg) =>
    set((s) => {
      const list = [...(s.messages[key] ?? [])];
      const last = lastAssistant(list);
      if (last) {
        last.streaming = false;
        last.error = msg;
      }
      return { messages: { ...s.messages, [key]: list }, streaming: { ...s.streaming, [key]: false } };
    }),
  setStreaming: (key, on) =>
    set((s) => ({ streaming: { ...s.streaming, [key]: on } })),
  clear: (key) =>
    set((s) => {
      const m = { ...s.messages };
      delete m[key];
      return { messages: m };
    }),
  hydrate: (key, msgs) =>
    set((s) => {
      // Only hydrate if we don't already have content for this key —
      // avoids clobbering an in-flight stream with stale disk state.
      if (s.messages[key] && s.messages[key]!.length > 0) return s;
      return { messages: { ...s.messages, [key]: msgs } };
    }),
}));

// ── Transcript hydration ────────────────────────────────────────────
// Convert raw Anthropic-format messages (what the backend persists on
// disk) back into the ChatMessage[] shape the UI renders. This lets us
// rehydrate a session after a page reload without losing history.
//
// Raw shape (Anthropic):
//   {"role":"user","content":"text"}                    ← visible user msg
//   {"role":"assistant","content":[{"type":"text",...},
//                                  {"type":"tool_use",...}]}
//   {"role":"user","content":[{"type":"tool_result",...}]}  ← internal, skipped
//
// We merge each assistant message's text + tool_use blocks into ONE
// ChatMessage, then pair tool_use ids with their tool_result in the
// following user message.

export interface RawBlock {
  type: string;
  text?: string;
  id?: string;
  name?: string;
  input?: Record<string, unknown>;
  tool_use_id?: string;
  content?: unknown;
  is_error?: boolean;
}

export interface RawMessage {
  role: "user" | "assistant";
  content: string | RawBlock[];
}

export function rawToChatMessages(raw: RawMessage[]): ChatMessage[] {
  const out: ChatMessage[] = [];
  for (let i = 0; i < raw.length; i++) {
    const m = raw[i];
    if (m.role === "user") {
      // String content is a visible user turn. Array content holds
      // tool_results that we pair into the preceding assistant's
      // activities below — skip here.
      if (typeof m.content === "string") {
        out.push({ role: "user", text: m.content });
      }
      continue;
    }
    if (m.role !== "assistant") continue;
    const blocks: RawBlock[] = Array.isArray(m.content) ? m.content : [];
    const text = blocks
      .filter((b) => b.type === "text" && typeof b.text === "string")
      .map((b) => b.text!)
      .join("");
    const toolUses = blocks.filter((b) => b.type === "tool_use");

    // Look ahead for tool_result blocks in the next message.
    const next = raw[i + 1];
    const results: Record<string, string> = {};
    if (next && next.role === "user" && Array.isArray(next.content)) {
      for (const b of next.content) {
        if (b.type === "tool_result" && b.tool_use_id) {
          results[b.tool_use_id] =
            typeof b.content === "string"
              ? b.content
              : Array.isArray(b.content)
                ? b.content
                    .map((x) =>
                      typeof x === "string"
                        ? x
                        : (x as { text?: string })?.text ?? JSON.stringify(x),
                    )
                    .join("")
                : b.content == null
                  ? ""
                  : JSON.stringify(b.content);
        }
      }
    }

    const activities: ChatActivity[] = toolUses.map((b) => ({
      kind: "tool_use",
      id: b.id ?? "",
      name: b.name ?? "",
      input: b.input ?? {},
      result: results[b.id ?? ""],
      expanded: false,
    }));

    out.push({
      role: "assistant",
      text,
      streaming: false,
      notices: [],
      activities,
    });
  }
  return out;
}

// ── Todos store ──────────────────────────────────────────────────────
// Separate from useChat so message streaming doesn't re-render the task
// board on every text delta. Keyed by `${pid}/${sid}` so switching
// sessions doesn't leak state across them.

interface TodosState {
  byKey: Record<string, TodoItem[]>;
  collapsed: Record<string, boolean>;
  setTodos: (key: string, todos: TodoItem[]) => void;
  hydrate: (key: string, todos: TodoItem[]) => void;
  toggleCollapsed: (key: string) => void;
  clear: (key: string) => void;
}

export const useTodos = create<TodosState>((set) => ({
  byKey: {},
  collapsed: {},
  setTodos: (key, todos) =>
    set((s) => ({ byKey: { ...s.byKey, [key]: todos } })),
  hydrate: (key, todos) =>
    set((s) => ({
      // Don't overwrite if we already have data (live SSE beat the cold
      // GET to the punch) — live data is always at least as fresh.
      byKey: key in s.byKey ? s.byKey : { ...s.byKey, [key]: todos },
    })),
  toggleCollapsed: (key) =>
    set((s) => ({
      collapsed: { ...s.collapsed, [key]: !s.collapsed[key] },
    })),
  clear: (key) =>
    set((s) => {
      const next = { ...s.byKey };
      delete next[key];
      return { byKey: next };
    }),
}));
