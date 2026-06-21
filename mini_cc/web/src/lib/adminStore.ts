import { create } from "zustand";
import type { KeyOut, MetricSnapshot } from "./types";
import type { AdminProfile } from "./api";
import {
  ApiError,
  DEFAULT_BASE,
  adminCreateKey,
  adminListKeys,
  adminMetrics,
  adminRevokeKey,
  adminRotateKey,
  adminUpdateKey,
  verifyAdmin,
} from "./api";

const LS_KEY = "mini_cc.admin.v1";

interface AdminState {
  profile: AdminProfile | null;
  keys: KeyOut[];
  metrics: MetricSnapshot | null;
  // Ring buffer of recent snapshots for sparklines.
  metricsHistory: { ts: number; snapshot: MetricSnapshot }[];
  busy: boolean;
  error: string | null;
  login: (baseUrl: string, tenantId: string, apiKey: string) => Promise<boolean>;
  logout: () => void;
  load: () => void;
  refresh: () => Promise<void>;
  refreshMetrics: () => Promise<void>;
  createKey: (body: { scopes?: string[]; expires_in?: string; label?: string }) => Promise<KeyOut>;
  patchKey: (
    key: string,
    body: { scopes?: string[]; expires_in?: string; label?: string },
  ) => Promise<KeyOut>;
  revokeKey: (key: string) => Promise<void>;
  rotateKey: (
    key: string,
    body: { grace_hours?: number; scopes?: string[]; expires_in?: string; label?: string },
  ) => Promise<{ new_key: KeyOut; old_key: KeyOut | null }>;
}

function persist(p: AdminProfile) {
  localStorage.setItem(LS_KEY, JSON.stringify(p));
}

function loadPersisted(): AdminProfile | null {
  try {
    const raw = localStorage.getItem(LS_KEY);
    return raw ? (JSON.parse(raw) as AdminProfile) : null;
  } catch {
    return null;
  }
}

const HISTORY_LIMIT = 30;

function pushSnapshot(
  hist: { ts: number; snapshot: MetricSnapshot }[],
  snap: MetricSnapshot,
) {
  const next = [...hist, { ts: Date.now(), snapshot: snap }];
  if (next.length > HISTORY_LIMIT) next.shift();
  return next;
}

function errMsg(e: unknown): string {
  if (e instanceof ApiError) return `${e.code}: ${e.message}`;
  return (e as Error).message;
}

export const useAdmin = create<AdminState>((set, get) => ({
  profile: null,
  keys: [],
  metrics: null,
  metricsHistory: [],
  busy: false,
  error: null,

  load: () => {
    const p = loadPersisted();
    if (p) set({ profile: p });
  },

  login: async (baseUrlRaw, tenantId, apiKey) => {
    const baseUrl = baseUrlRaw.trim() || DEFAULT_BASE;
    const p: AdminProfile = {
      baseUrl,
      tenantId: tenantId.trim(),
      apiKey: apiKey.trim(),
    };
    set({ busy: true, error: null });
    try {
      const keys = await verifyAdmin(p);
      persist(p);
      set({ profile: p, keys, busy: false });
      // Kick off metrics fetch in background; ignore errors.
      void get().refreshMetrics().catch(() => {});
      return true;
    } catch (e) {
      set({ busy: false, error: errMsg(e) });
      return false;
    }
  },

  logout: () => {
    localStorage.removeItem(LS_KEY);
    set({ profile: null, keys: [], metrics: null, metricsHistory: [], error: null });
  },

  refresh: async () => {
    const p = get().profile;
    if (!p) return;
    set({ busy: true, error: null });
    try {
      const keys = await adminListKeys(p);
      set({ keys, busy: false });
    } catch (e) {
      set({ busy: false, error: errMsg(e) });
    }
  },

  refreshMetrics: async () => {
    const p = get().profile;
    if (!p) return;
    try {
      const snap = await adminMetrics(p);
      set((s) => ({
        metrics: snap,
        metricsHistory: pushSnapshot(s.metricsHistory, snap),
      }));
    } catch (e) {
      set({ error: errMsg(e) });
    }
  },

  createKey: async (body) => {
    const p = get().profile;
    if (!p) throw new Error("not logged in");
    const rec = await adminCreateKey(p, body);
    set((s) => ({ keys: [...s.keys, rec] }));
    return rec;
  },

  patchKey: async (key, body) => {
    const p = get().profile;
    if (!p) throw new Error("not logged in");
    const rec = await adminUpdateKey(p, key, body);
    set((s) => ({ keys: s.keys.map((k) => (k.key === key ? rec : k)) }));
    return rec;
  },

  revokeKey: async (key) => {
    const p = get().profile;
    if (!p) throw new Error("not logged in");
    await adminRevokeKey(p, key);
    set((s) => ({ keys: s.keys.filter((k) => k.key !== key) }));
  },

  rotateKey: async (key, body) => {
    const p = get().profile;
    if (!p) throw new Error("not logged in");
    const out = await adminRotateKey(p, key, body);
    set((s) => {
      // If grace_hours=0 (hard revoke), old key removed.
      if (out.old_key === null) {
        return { keys: [...s.keys.filter((k) => k.key !== key), out.new_key] };
      }
      return { keys: [...s.keys.filter((k) => k.key !== key), out.new_key, out.old_key] };
    });
    return out;
  },
}));
