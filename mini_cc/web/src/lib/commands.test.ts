import { describe, it, expect, afterEach, beforeEach, vi } from "vitest";
import { runCommandForCard, streamRunCommand } from "./commands";
import type { CardEvent, TenantProfile } from "./types";

const profile: TenantProfile = {
  label: "t",
  tenantId: "t",
  apiKey: "k",
  baseUrl: "",
};

function makeSseBody(events: unknown[]): string {
  return events
    .map((e) => `id: 1\ndata: ${JSON.stringify(e)}\n\n`)
    .join("") + "data: [DONE]\n\n";
}

function mockFetch(events: unknown[]) {
  const calls: { url: string; init?: RequestInit }[] = [];
  const fake = vi.fn(async (url: string, init?: RequestInit) => {
    calls.push({ url, init });
    const body = makeSseBody(events);
    const stream = new ReadableStream<Uint8Array>({
      start(controller) {
        controller.enqueue(new TextEncoder().encode(body));
        controller.close();
      },
    });
    return new Response(stream, {
      status: 200,
      headers: { "Content-Type": "text/event-stream" },
    });
  });
  vi.stubGlobal("fetch", fake);
  return {
    calls,
    lastBody: () => {
      const init = calls[calls.length - 1]?.init;
      if (!init?.body) return null;
      try {
        return JSON.parse(init.body as string);
      } catch {
        return null;
      }
    },
  };
}

describe("runCommandForCard", () => {
  beforeEach(() => {
    vi.stubGlobal("window", globalThis);
  });
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("resolves with the first card event", async () => {
    const card: CardEvent = {
      id: "agents-roster",
      variant: "list",
      title: "Teammates",
      icon: "agents",
      status: "ok",
      payload: { items: [] },
      actions: [],
      emitted_at: 0,
      revision: 1,
    };
    mockFetch([{ type: "card", ...card }]);

    const result = await runCommandForCard(profile, "p", "s", "agents");
    expect(result?.id).toBe("agents-roster");
    expect(result?.variant).toBe("list");
  });

  it("sends ephemeral=true in the request body (regression: arg-order bug)", async () => {
    // Bug: streamRunCommand's signature used to be
    //   (profile, pid, sid, name, handlers, args, signal?, options?)
    // and runCommandForCard called it with (..., args, { ephemeral: true }).
    // The options object was passed as `signal`, fetch tried to convert
    // it to an AbortSignal and threw synchronously — every panel poll
    // failed silently. Test pins the new arg order so it can't regress.
    const tracker = mockFetch([]);

    await runCommandForCard(profile, "p", "s", "agents", "");
    const body = tracker.lastBody();
    expect(body).not.toBeNull();
    expect(body?.ephemeral).toBe(true);
  });

  it("does not throw synchronously when called without args", async () => {
    // The original bug surfaced as a synchronous throw inside fetch
    // because { ephemeral: true } couldn't be converted to AbortSignal.
    // A successful promise return (resolved or rejected) proves the
    // signature is correct.
    mockFetch([]);
    await expect(
      runCommandForCard(profile, "p", "s", "agents"),
    ).resolves.toBeNull();
  });
});

describe("streamRunCommand arg order", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("accepts options without signal (options before signal)", async () => {
    const tracker = mockFetch([]);

    await streamRunCommand(
      profile,
      "p",
      "s",
      "agents",
      { onEvent: () => {} },
      "",
      { ephemeral: true },
    );

    const body = tracker.lastBody();
    expect(body?.ephemeral).toBe(true);
  });

  it("accepts signal after options", async () => {
    const tracker = mockFetch([]);
    const controller = new AbortController();

    await streamRunCommand(
      profile,
      "p",
      "s",
      "agents",
      { onEvent: () => {} },
      "",
      { ephemeral: false },
      controller.signal,
    );

    const init = tracker.calls[tracker.calls.length - 1]?.init;
    expect(init?.signal).toBe(controller.signal);
    expect(tracker.lastBody()?.ephemeral).toBe(false);
  });
});
