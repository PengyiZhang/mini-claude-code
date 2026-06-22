import { ApiError, parseErr, tenantPath, authHeaders } from "./api";
import type { SendEvent, TenantProfile } from "./types";

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
