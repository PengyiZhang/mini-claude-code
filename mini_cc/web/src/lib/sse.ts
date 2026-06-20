import type { SendEvent } from "./types";
import { ApiError } from "./api";

/**
 * SSE client tailored to mini_cc's /send endpoint.
 *
 * Uses fetch + ReadableStream (not EventSource) so we can POST a body
 * and pass Bearer auth. Parses the `data: <json>\n\n` framing, including
 * the trailing `data: [DONE]` sentinel.
 *
 * Returns an AbortController via the second arg so callers can stop the
 * stream on unmount.
 */
export interface SSEHandlers {
  onEvent: (ev: SendEvent) => void;
  onError?: (err: Error) => void;
  onDone?: () => void;
}

export interface SSERequest {
  url: string;
  apiKey: string;
  body: unknown;
  signal?: AbortSignal;
}

export async function streamSend(req: SSERequest, h: SSEHandlers): Promise<void> {
  let res: Response;
  try {
    res = await fetch(req.url, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Authorization: `Bearer ${req.apiKey}`,
        Accept: "text/event-stream",
      },
      body: JSON.stringify(req.body),
      signal: req.signal,
    });
  } catch (e) {
    h.onError?.(e as Error);
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
    h.onError?.(new ApiError(res.status, "sse_error", msg));
    return;
  }
  if (!res.body) {
    h.onError?.(new Error("no response body"));
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
        if (!raw) continue;
        if (!raw.startsWith("data:")) continue;
        const payload = raw.slice(5).trim();
        if (payload === "[DONE]") {
          h.onDone?.();
          return;
        }
        try {
          const ev = JSON.parse(payload) as SendEvent;
          h.onEvent(ev);
        } catch (e) {
          // swallow individual parse errors; keep streaming
        }
      }
    }
    h.onDone?.();
  } catch (e) {
    if ((e as Error).name === "AbortError") {
      h.onDone?.();
      return;
    }
    h.onError?.(e as Error);
  }
}
