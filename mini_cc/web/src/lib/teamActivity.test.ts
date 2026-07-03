import { describe, it, expect, afterEach, beforeEach, vi } from "vitest";
import { useTeamActivity } from "./teamActivity";
import type { TenantProfile } from "./types";

const profile: TenantProfile = {
  label: "t",
  tenantId: "t",
  apiKey: "k",
  baseUrl: "http://test",
};

interface MockCall {
  url: string;
  init?: RequestInit;
}

function mockFetchOnce(body: unknown, opts?: { status?: number }): {
  calls: MockCall[];
} {
  const calls: MockCall[] = [];
  const status = opts?.status ?? 200;
  const fake = vi.fn(async (url: string, init?: RequestInit) => {
    calls.push({ url, init });
    return new Response(JSON.stringify(body), {
      status,
      headers: { "Content-Type": "application/json" },
    });
  });
  vi.stubGlobal("fetch", fake);
  return { calls };
}

function mockFetchReject(error: unknown): { calls: MockCall[] } {
  const calls: MockCall[] = [];
  const fake = vi.fn(async (url: string, init?: RequestInit) => {
    calls.push({ url, init });
    throw error instanceof Error ? error : new Error(String(error));
  });
  vi.stubGlobal("fetch", fake);
  return { calls };
}

beforeEach(() => {
  // Reset store state between tests so state doesn't leak.
  useTeamActivity.setState({
    perProjectActivity: {},
    since: {},
    busy: {},
    error: {},
  });
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("useTeamActivity.poll", () => {
  it("poll merges new events into existing list and updates cursor", async () => {
    const events = [
      { session_id: "s1", ts: "2026-07-04T10:00:00Z", type: "text", text: "a" },
      { session_id: "s1", ts: "2026-07-04T10:00:01Z", type: "text", text: "b" },
      { session_id: "s1", ts: "2026-07-04T10:00:02Z", type: "text", text: "c" },
    ];
    const { calls } = mockFetchOnce({ events, has_more: false });

    await useTeamActivity.getState().poll(profile, "p1");

    const state = useTeamActivity.getState();
    expect(state.perProjectActivity["p1"]).toHaveLength(3);
    expect(state.perProjectActivity["p1"]).toEqual(events);
    // cursor = last event's ts (sorted ascending)
    expect(state.since["p1"]).toBe("2026-07-04T10:00:02Z");
    expect(state.busy["p1"]).toBe(false);
    expect(state.error["p1"]).toBeNull();
    // sanity: hit the right URL
    expect(calls[0].url).toContain("/projects/p1/team/activity");
  });

  it("poll with cursor uses since param", async () => {
    // Pre-seed cursor
    useTeamActivity.setState({ since: { p1: "2026-07-04T09:00:00Z" } });
    const { calls } = mockFetchOnce({ events: [], has_more: false });

    await useTeamActivity.getState().poll(profile, "p1");

    const url = calls[0]?.url ?? "";
    expect(url).toContain("since=2026-07-04T09%3A00%3A00Z");
  });

  it("poll dedups same-key events", async () => {
    const e1 = {
      session_id: "s1",
      ts: "2026-07-04T10:00:00Z",
      type: "text",
      text: "a",
    };
    const e2 = {
      session_id: "s1",
      ts: "2026-07-04T10:00:01Z",
      type: "text",
      text: "b",
    };
    // Pre-seed with E1
    useTeamActivity.setState({
      perProjectActivity: { p1: [e1] },
      since: { p1: e1.ts },
    });
    // Backend returns [E1, E2] — E1 dup
    mockFetchOnce({ events: [e1, e2], has_more: false });

    await useTeamActivity.getState().poll(profile, "p1");

    const list = useTeamActivity.getState().perProjectActivity["p1"];
    expect(list).toHaveLength(2);
    expect(list).toEqual([e1, e2]);
  });

  it("poll sorts merged list by ts ascending", async () => {
    const late = {
      session_id: "s1",
      ts: "2026-07-04T10:00:05Z",
      type: "text",
      text: "late",
    };
    const early = {
      session_id: "s1",
      ts: "2026-07-04T10:00:01Z",
      type: "text",
      text: "early",
    };
    // Pre-seed with a late event
    useTeamActivity.setState({
      perProjectActivity: { p1: [late] },
      since: { p1: late.ts },
    });
    // Backend returns an earlier event — final list must be ascending
    mockFetchOnce({ events: [early], has_more: false });

    await useTeamActivity.getState().poll(profile, "p1");

    const list = useTeamActivity.getState().perProjectActivity["p1"];
    expect(list.map((e) => e.text)).toEqual(["early", "late"]);
  });

  it("poll busy guard prevents concurrent calls", async () => {
    // Two events so we can resolve them sequentially; the second call
    // must be skipped because busy[pid] is true.
    const events1 = [
      { session_id: "s1", ts: "2026-07-04T10:00:00Z", type: "text", text: "a" },
    ];
    const events2 = [
      { session_id: "s1", ts: "2026-07-04T10:00:01Z", type: "text", text: "b" },
    ];

    let resolveFirst: (v: Response) => void = () => {};
    const firstPromise = new Promise<Response>((res) => {
      resolveFirst = res;
    });
    const fake = vi.fn(async (url: string, _init?: RequestInit) => {
      // First call hangs until we resolve; second call (if made) returns fast.
      if (fake.mock.calls.length === 1) {
        return firstPromise;
      }
      return new Response(JSON.stringify({ events: events2, has_more: false }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      });
    });
    vi.stubGlobal("fetch", fake);

    // Start first poll (doesn't resolve yet).
    const p1 = useTeamActivity.getState().poll(profile, "p1");
    // Kick off second concurrently — should be skipped due to busy guard.
    const p2 = useTeamActivity.getState().poll(profile, "p1");

    // Resolve the first fetch with events1.
    resolveFirst(
      new Response(JSON.stringify({ events: events1, has_more: false }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    );

    await Promise.all([p1, p2]);

    // Only one fetch should have been issued — busy guard skipped the second.
    expect(fake).toHaveBeenCalledTimes(1);
    // And the store contains only events1 (events2 never fetched).
    const list = useTeamActivity.getState().perProjectActivity["p1"];
    expect(list).toEqual(events1);
  });

  it("poll failure sets error and clears busy", async () => {
    mockFetchReject(new Error("boom"));

    await useTeamActivity.getState().poll(profile, "p1");

    const state = useTeamActivity.getState();
    expect(state.busy["p1"]).toBe(false);
    expect(state.error["p1"]).toBe("boom");
    // Existing activity untouched — never set means undefined (store
    // only allocates per-pid state on first successful poll).
    expect(state.perProjectActivity["p1"]).toBeUndefined();
  });
});

describe("useTeamActivity.reset", () => {
  it("reset clears state for one project but not others", () => {
    const ea = [
      { session_id: "s1", ts: "2026-07-04T10:00:00Z", type: "text", text: "a" },
    ];
    const eb = [
      { session_id: "s2", ts: "2026-07-04T10:00:01Z", type: "text", text: "b" },
    ];
    useTeamActivity.setState({
      perProjectActivity: { pa: ea, pb: eb },
      since: { pa: "2026-07-04T10:00:00Z", pb: "2026-07-04T10:00:01Z" },
      busy: { pa: false, pb: false },
      error: { pa: null, pb: null },
    });

    useTeamActivity.getState().reset("pa");

    const state = useTeamActivity.getState();
    expect(state.perProjectActivity["pa"]).toBeUndefined();
    expect(state.perProjectActivity["pb"]).toEqual(eb);
    expect(state.since["pa"]).toBeUndefined();
    expect(state.since["pb"]).toBe("2026-07-04T10:00:01Z");
  });
});
