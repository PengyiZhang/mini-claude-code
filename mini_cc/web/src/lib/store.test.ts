import { describe, it, expect, beforeEach } from "vitest";
import { useChat, rawToChatMessages } from "./store";
import type { CardEvent } from "./types";

function card(overrides: Partial<CardEvent> = {}): CardEvent {
  return {
    id: "c",
    variant: "list",
    status: "ok",
    payload: { items: [] },
    actions: [],
    emitted_at: 0,
    revision: 1,
    ...overrides,
  };
}

describe("useChat.addCard", () => {
  beforeEach(() => {
    useChat.getState().clear("p/s");
    useChat.getState().clear("p/s2");
  });

  it("appends a card to the last assistant message", () => {
    const key = "p/s";
    useChat.getState().startAssistant(key);
    useChat.getState().addCard(key, card({ id: "c1" }));
    const msgs = useChat.getState().messages[key];
    expect(msgs[msgs.length - 1].cards?.map((c) => c.id)).toEqual(["c1"]);
  });

  it("replaces an existing card with the same id (replace-by-id)", () => {
    const key = "p/s2";
    useChat.getState().startAssistant(key);
    useChat.getState().addCard(key, card({ id: "c", revision: 1 }));
    useChat.getState().addCard(
      key,
      card({
        id: "c",
        revision: 2,
        payload: { items: [{ id: "i", title: "x", badges: [], menu: [] }] },
      }),
    );
    const msgs = useChat.getState().messages[key];
    expect(msgs[msgs.length - 1].cards).toHaveLength(1);
    expect(msgs[msgs.length - 1].cards?.[0].revision).toBe(2);
  });

  it("does nothing when no assistant message exists", () => {
    const key = "p/s";
    useChat.getState().addCard(key, card({ id: "orphan" }));
    const msgs = useChat.getState().messages[key];
    // No bubble was opened → nothing to attach to.
    expect(msgs ?? []).toHaveLength(0);
  });
});

describe("useChat.replaceCardEverywhere", () => {
  beforeEach(() => {
    useChat.getState().clear("p/s");
  });

  it("replaces a card by id across all messages, not just the last", () => {
    // Live-refresh invariant: a /bg refresh tick must swap the stale
    // card in-place, even if newer user/assistant turns have landed
    // between the original render and the poll. addCard can't do this
    // (it only looks at the last assistant bubble), so we route
    // refreshes through replaceCardEverywhere instead.
    const key = "p/s";
    useChat.getState().startAssistant(key);
    useChat.getState().addCard(key, card({ id: "bg", revision: 1 }));
    // Simulate a newer turn arriving after the card was rendered.
    useChat.getState().appendUser(key, "/model");
    useChat.getState().startAssistant(key);
    useChat.getState().appendText(key, "ok");
    useChat.getState().finishAssistant(key);

    useChat.getState().replaceCardEverywhere(
      key,
      "bg",
      card({ id: "bg", revision: 2, payload: { items: [{ id: "x", title: "y", badges: [], menu: [] }] } }),
    );

    const msgs = useChat.getState().messages[key];
    const firstBubble = msgs[0];
    expect(firstBubble.cards?.[0].revision).toBe(2);
    expect(firstBubble.cards?.[0].payload).toEqual({
      items: [{ id: "x", title: "y", badges: [], menu: [] }],
    });
  });

  it("is a no-op when the card id is not present", () => {
    const key = "p/s";
    useChat.getState().startAssistant(key);
    useChat.getState().addCard(key, card({ id: "bg", revision: 1 }));
    const before = useChat.getState().messages[key];
    useChat.getState().replaceCardEverywhere(
      key,
      "nonexistent",
      card({ id: "nonexistent", revision: 2 }),
    );
    expect(useChat.getState().messages[key]).toBe(before);
  });
});

describe("rawToChatMessages — __card__ hydration", () => {
  it("hydrates __card__ tool_use blocks into msg.cards", () => {
    const raw = [
      { role: "user", content: "/config" },
      {
        role: "assistant",
        content: [
          {
            type: "tool_use",
            id: "t1",
            name: "__card__",
            input: {
              id: "config",
              variant: "key_value",
              status: "ok",
              payload: { pairs: [] },
              actions: [],
              emitted_at: 0,
              revision: 1,
            },
          },
        ],
      },
      {
        role: "user",
        content: [{ type: "tool_result", tool_use_id: "t1", content: "ok" }],
      },
    ];
    const out = rawToChatMessages(raw as never);
    // The card should land on the assistant bubble, not as an activity.
    const asst = out.find((m) => m.role === "assistant");
    expect(asst?.cards?.[0].id).toBe("config");
    expect(asst?.cards?.[0].variant).toBe("key_value");
    // The synthetic tool_result must NOT leak as an activity entry.
    expect(asst?.activities ?? []).toHaveLength(0);
  });

  it("mixes cards and real tool_use blocks in one bubble", () => {
    const raw = [
      { role: "user", content: "/agents" },
      {
        role: "assistant",
        content: [
          {
            type: "tool_use",
            id: "card-1",
            name: "__card__",
            input: {
              id: "roster",
              variant: "list",
              status: "ok",
              payload: { items: [] },
              actions: [],
              emitted_at: 0,
              revision: 1,
            },
          },
          {
            type: "tool_use",
            id: "real-1",
            name: "read_file",
            input: { path: "/tmp/x" },
          },
        ],
      },
      {
        role: "user",
        content: [
          { type: "tool_result", tool_use_id: "card-1", content: "ok" },
          { type: "tool_result", tool_use_id: "real-1", content: "data" },
        ],
      },
    ];
    const out = rawToChatMessages(raw as never);
    const asst = out.find((m) => m.role === "assistant");
    expect(asst?.cards?.[0].id).toBe("roster");
    expect(asst?.activities?.[0].name).toBe("read_file");
    expect(asst?.activities?.[0].result).toBe("data");
  });
});

// Bug 2 follow-up: hydrate-time dedup for adjacent same-id card bubbles.
// TeammatesPanel polls /agents every 4s; pre-ephemeral-flag, every poll
// persisted one __card__ bubble to disk. After refresh, the chat pane
// filled with N stale roster snapshots. The dedup walks the assembled
// message list and collapses runs of adjacent bubbles whose only
// content is a single card with the same id — keeps the latest
// snapshot, drops the orphaned older bubbles. Real usage (text or
// other content between invocations) survives because the run is
// broken by the interleaved content.
describe("rawToChatMessages — adjacent same-id card dedup", () => {
  const rosterBubble = (id: string, revision: number) => ({
    role: "assistant",
    content: [
      {
        type: "tool_use",
        id: `card-${revision}`,
        name: "__card__",
        input: {
          id,
          variant: "list",
          status: "ok",
          payload: { items: [] },
          actions: [],
          emitted_at: 0,
          revision,
        },
      },
    ],
  });
  const toolResultFor = (revision: number) => ({
    role: "user",
    content: [
      { type: "tool_result", tool_use_id: `card-${revision}`, content: "ok" },
    ],
  });

  it("collapses three adjacent roster snapshots into the latest one", () => {
    const raw = [
      { role: "user", content: "/agents" },
      rosterBubble("agents-roster", 1),
      toolResultFor(1),
      rosterBubble("agents-roster", 2),
      toolResultFor(2),
      rosterBubble("agents-roster", 3),
      toolResultFor(3),
    ];
    const out = rawToChatMessages(raw as never);
    const assistants = out.filter((m) => m.role === "assistant");
    expect(assistants).toHaveLength(1);
    expect(assistants[0].cards).toHaveLength(1);
    expect(assistants[0].cards?.[0].revision).toBe(3);
  });

  it("preserves bubbles separated by real user text", () => {
    const raw = [
      { role: "user", content: "/agents" },
      rosterBubble("agents-roster", 1),
      toolResultFor(1),
      // A real user message between two /agents calls — both should
      // survive because the user explicitly re-ran the command.
      { role: "user", content: "what does the roster look like now?" },
      { role: "user", content: "/agents" },
      rosterBubble("agents-roster", 2),
      toolResultFor(2),
    ];
    const out = rawToChatMessages(raw as never);
    const assistants = out.filter((m) => m.role === "assistant");
    expect(assistants).toHaveLength(2);
  });

  it("does not collapse bubbles with different card ids", () => {
    // rawToChatMessages merges consecutive assistant messages with no
    // real user turn between them into a single bubble — so two
    // different-id cards land in the SAME bubble's cards array. The
    // dedup must keep both (they're not duplicates).
    const raw = [
      { role: "user", content: "/agents" },
      rosterBubble("agents-roster", 1),
      toolResultFor(1),
      rosterBubble("workflow-runs", 1),
      toolResultFor(1),
    ];
    const out = rawToChatMessages(raw as never);
    const assistants = out.filter((m) => m.role === "assistant");
    expect(assistants).toHaveLength(1);
    const ids = assistants[0].cards?.map((c) => c.id);
    expect(ids).toContain("agents-roster");
    expect(ids).toContain("workflow-runs");
  });
});

// Regression: persisted transcripts from before commit 6b0157b contain
// __card__ blocks in the legacy nested shape {card:{kind,items,...}}
// instead of the flat CardEvent shape. rawToChatMessages must NOT pass
// those through as CardEvent — they lack variant/id/status, so
// CardShell reads STATUS_BADGE[undefined] and crashes on render.
// Drop them at hydrate time; the backend no longer emits this shape.
describe("rawToChatMessages — legacy nested-shape cards", () => {
  it("skips __card__ blocks missing variant/id/status", () => {
    const raw = [
      { role: "user", content: "/agents inbox alice" },
      {
        role: "assistant",
        content: [
          {
            type: "tool_use",
            id: "card_legacy",
            name: "__card__",
            // Legacy nested shape — what OLD registry.py:1155 emitted
            // before the CardEvent rewrite.
            input: {
              card: {
                kind: "list",
                title: "Inbox · @alice (1)",
                items: [{ id: "alice-inbox-1" }],
                expandable_command: "/agents inbox alice ack <ts>",
              },
            },
          },
        ],
      },
      {
        role: "user",
        content: [
          { type: "tool_result", tool_use_id: "card_legacy", content: "ok" },
        ],
      },
    ];
    const out = rawToChatMessages(raw as never);
    const asst = out.find((m) => m.role === "assistant");
    // The stale snapshot is dropped — no card rendered, no crash.
    expect(asst?.cards ?? []).toHaveLength(0);
  });
});
