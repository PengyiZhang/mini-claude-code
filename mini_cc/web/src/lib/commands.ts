import { ApiError, parseErr, tenantPath, authHeaders } from "./api";
import type { CardEvent, SendEvent, TenantProfile } from "./types";

export interface CommandDef {
  name: string;
  description: string;
  scope: "client" | "server";
  aliases: string[];
}

export async function fetchCommands(
  profile: TenantProfile,
  pid: string,
  sid: string,
): Promise<CommandDef[]> {
  const res = await fetch(
    tenantPath(profile, `/projects/${pid}/sessions/${sid}/commands`),
    { headers: authHeaders(profile) },
  );
  if (!res.ok) await parseErr(res);
  return res.json();
}

export async function streamRunCommand(
  profile: TenantProfile,
  pid: string,
  sid: string,
  name: string,
  handlers: {
    onEvent: (ev: SendEvent) => void;
    onError?: (err: Error) => void;
    onDone?: () => void;
  },
  args?: string,
  signal?: AbortSignal,
): Promise<void> {
  let res: Response;
  try {
    res = await fetch(
      tenantPath(profile, `/projects/${pid}/sessions/${sid}/commands/${encodeURIComponent(name)}`),
      {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          Accept: "text/event-stream",
          ...authHeaders(profile),
        },
        body: JSON.stringify({ args: args ?? "" }),
        signal,
      },
    );
  } catch (e) {
    if ((e as Error).name === "AbortError") {
      handlers.onDone?.();
      return;
    }
    handlers.onError?.(e as Error);
    return;
  }

  if (!res.ok) {
    let msg = `HTTP ${res.status}`;
    try {
      const body = await res.json();
      msg = body?.error?.message ?? msg;
    } catch {
      /* ignore */
    }
    handlers.onError?.(new ApiError(res.status, "command_error", msg));
    return;
  }
  if (!res.body) {
    handlers.onError?.(new Error("no response body"));
    return;
  }

  const reader = res.body.getReader();
  const decoder = new TextDecoder("utf-8");
  let buf = "";

  try {
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      let nl: number;
      while ((nl = buf.indexOf("\n")) !== -1) {
        const raw = buf.slice(0, nl).trim();
        buf = buf.slice(nl + 1);
        if (!raw || !raw.startsWith("data:")) continue;
        const payload = raw.slice(5).trim();
        if (payload === "[DONE]") {
          handlers.onDone?.();
          return;
        }
        try {
          handlers.onEvent(JSON.parse(payload) as SendEvent);
        } catch {
          /* swallow individual parse errors */
        }
      }
    }
    handlers.onDone?.();
  } catch (e) {
    if ((e as Error).name === "AbortError") {
      handlers.onDone?.();
      return;
    }
    handlers.onError?.(e as Error);
  }
}

/**
 * Fire-and-refetch helper for panel actions: run a server command to
 * completion and resolve with the concatenated text payload. Panels call
 * this for side effects (e.g. `/workflow save`, `/bg stop <id>`) then
 * re-fetch their JSON view. Rejects on stream error.
 */
export async function runCommandOnce(
  profile: TenantProfile,
  pid: string,
  sid: string,
  name: string,
  args?: string,
): Promise<string> {
  return new Promise((resolve, reject) => {
    let acc = "";
    void streamRunCommand(
      profile,
      pid,
      sid,
      name,
      {
        onEvent: (ev) => {
          if (ev.type === "text") acc += ev.text;
          if (ev.type === "error") reject(new Error(ev.message));
        },
        onError: (e) => reject(e),
        onDone: () => resolve(acc),
      },
      args,
    );
  });
}

/**
 * Live-refresh helper: re-run a slash command silently and resolve with
 * the first card event it emits (or null if the handler emits no card).
 * Used by the CardView polling hook so /bg + /agents rosters refresh
 * in-place via replaceCardEverywhere instead of stacking new bubbles.
 *
 * The slash command runs without writing user-input markers and the
 * result is consumed programmatically; no assistant bubble is created
 * on the client side — the caller routes the resulting card straight
 * into the store via replaceCardEverywhere.
 */
export async function runCommandForCard(
  profile: TenantProfile,
  pid: string,
  sid: string,
  name: string,
  args?: string,
): Promise<CardEvent | null> {
  return new Promise((resolve, reject) => {
    let card: CardEvent | null = null;
    void streamRunCommand(
      profile,
      pid,
      sid,
      name,
      {
        onEvent: (ev) => {
          if (ev.type === "card" && !card) card = ev as CardEvent;
          if (ev.type === "error") reject(new Error((ev as { message: string }).message));
        },
        onError: (e) => reject(e),
        onDone: () => resolve(card),
      },
      args,
    );
  });
}
