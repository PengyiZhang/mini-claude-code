import { create } from "zustand";
import type { TenantProfile } from "./types";

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
}));
