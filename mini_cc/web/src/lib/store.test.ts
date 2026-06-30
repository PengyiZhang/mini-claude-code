import { describe, it, expect, beforeEach } from "vitest";
import { useChat } from "./store";
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
