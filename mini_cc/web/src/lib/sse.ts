import type { SendEvent } from "./types";
import { ApiError } from "./api";

/**
 * SSE client tailored to mini_cc's /send endpoint.
 *
 * Uses fetch + ReadableStream (not EventSource) so we can POST a body
 * and pass Bearer auth. Parses the `id: <seq>\ndata: <json>\n\n`
 * framing, including the trailing `data: [DONE]` sentinel.
 *
 * B8 auto-reconnect: when `reconnect: true` is set and the connection
 * drops after at least one event was received, the client retries with
 * `Last-Event-Id: <last_seq>` and `body.resume = true`. The server's
 * resume-only path replays events from the per-session log without
 * triggering a duplicate LLM run. Retries use exponential backoff
 * (1s → 2s → 4s) up to `maxRetries` (default 3).
 *
 * Returns an AbortController via the second arg so callers can stop the
 * stream on unmount.
 */
export interface SSEHandlers {
  onEvent: (ev: SendEvent) => void;
  onError?: (err: Error) => void;
  onDone?: () => void;
  /** Emitted when a reconnect attempt is scheduled (useful for UI). */
  onReconnect?: (info: { attempt: number; lastSeq: number; delayMs: number }) => void;
}

export interface SSERequest {
  url: string;
  apiKey: string;
  body: unknown;
  signal?: AbortSignal;
  /** Enable B8 auto-reconnect on mid-stream drops. Default: false. */
  reconnect?: boolean;
  /** Max reconnect attempts. Default: 3. */
  maxRetries?: number;
}

const RECONNECT_BASE_MS = 1000;

export async function streamSend(req: SSERequest, h: SSEHandlers): Promise<void> {
  const maxRetries = req.maxRetries ?? 3;
  let lastSeq = 0;
  let receivedAny = false;
  let attempt = 0;

  const sleep = (ms: number) =>
    new Promise<void>((resolve) => {
      const t = setTimeout(resolve, ms);
      req.signal?.addEventListener("abort", () => {
        clearTimeout(t);
        resolve();
      }, { once: true });
    });

  // Loop so we can reconnect on drop. The initial attempt and each
  // reconnect go through the same body.
  // eslint-disable-next-line no-constant-condition
  while (true) {
    if (req.signal?.aborted) {
      h.onDone?.();
      return;
    }
    const isResume = attempt > 0;
    const body = isResume
      ? { ...(req.body as object), resume: true }
      : req.body;
    const headers: Record<string, string> = {
      "Content-Type": "application/json",
      Authorization: `Bearer ${req.apiKey}`,
      Accept: "text/event-stream",
    };
    if (isResume) {
      headers["Last-Event-Id"] = String(lastSeq);
    }

    let res: Response;
    try {
      res = await fetch(req.url, {
        method: "POST",
        headers,
        body: JSON.stringify(body),
        signal: req.signal,
      });
    } catch (e) {
      if ((e as Error).name === "AbortError") {
        h.onDone?.();
        return;
      }
      // Network drop — try reconnect if we've received at least one
      // event before, otherwise surface the error.
      if (req.reconnect && receivedAny && attempt < maxRetries) {
        await doReconnect();
        continue;
      }
      h.onError?.(e as Error);
      return;
    }

    if (!res.ok) {
      let msg = `HTTP ${res.status}`;
      try {
        const bodyJson = await res.json();
        msg = bodyJson?.error?.message ?? msg;
      } catch {
        /* ignore */
      }
      // 4xx (other than 409) are deterministic — don't reconnect.
      // 5xx / 409 (project busy) are transient — retry if enabled.
      const transient = res.status >= 500 || res.status === 409;
      if (req.reconnect && transient && receivedAny && attempt < maxRetries) {
        await doReconnect();
        continue;
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
          // Parse SSE framing: `id: <seq>` updates lastSeq;
          // `data: <json|DONE>` is the event payload.
          if (raw.startsWith("id:")) {
            const idStr = raw.slice(3).trim();
            const n = parseInt(idStr, 10);
            if (!Number.isNaN(n)) lastSeq = n;
            continue;
          }
          if (!raw.startsWith("data:")) continue;
          const payload = raw.slice(5).trim();
          if (payload === "[DONE]") {
            h.onDone?.();
            return;
          }
          try {
            const ev = JSON.parse(payload) as SendEvent;
            receivedAny = true;
            h.onEvent(ev);
          } catch {
            // swallow individual parse errors; keep streaming
          }
        }
      }
      h.onDone?.();
      return;
    } catch (e) {
      if ((e as Error).name === "AbortError") {
        h.onDone?.();
        return;
      }
      // Stream broke mid-read. Try resume if enabled + we've received
      // at least one event from this session.
      if (req.reconnect && receivedAny && attempt < maxRetries) {
        await doReconnect();
        continue;
      }
      h.onError?.(e as Error);
      return;
    }
  }

  async function doReconnect() {
    attempt += 1;
    const delayMs = RECONNECT_BASE_MS * Math.pow(2, attempt - 1);
    h.onReconnect?.({ attempt, lastSeq, delayMs });
    await sleep(delayMs);
  }
}
