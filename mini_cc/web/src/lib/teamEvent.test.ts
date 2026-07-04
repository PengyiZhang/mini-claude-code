import { describe, it, expect } from "vitest";
import { mergeConsecutiveTexts } from "./teamEvent";
import type { TeamEvent } from "./types";

function textEvent(
  sessionId: string,
  ts: string,
  text: string,
): TeamEvent {
  return {
    session_id: sessionId,
    ts,
    type: "text",
    text,
  } as unknown as TeamEvent;
}

function otherEvent(
  sessionId: string,
  ts: string,
  type: string,
): TeamEvent {
  return {
    session_id: sessionId,
    ts,
    type,
  } as unknown as TeamEvent;
}

describe("mergeConsecutiveTexts", () => {
  it("merges adjacent text events from the same session", () => {
    const events: TeamEvent[] = [
      textEvent("teammate-alice", "2026-07-04T10:00:00Z", "hello "),
      textEvent("teammate-alice", "2026-07-04T10:00:01Z", "world"),
    ];
    const out = mergeConsecutiveTexts(events);
    expect(out).toHaveLength(1);
    expect(out[0]).toEqual({
      kind: "merged_text",
      sessionId: "teammate-alice",
      ts: "2026-07-04T10:00:00Z",
      text: "hello world",
    });
  });

  it("splits when session changes between text events", () => {
    const events: TeamEvent[] = [
      textEvent("teammate-alice", "2026-07-04T10:00:00Z", "alice says "),
      textEvent("teammate-alice", "2026-07-04T10:00:01Z", "hi"),
      textEvent("sess-lead", "2026-07-04T10:00:02Z", "lead "),
      textEvent("sess-lead", "2026-07-04T10:00:03Z", "replies"),
    ];
    const out = mergeConsecutiveTexts(events);
    expect(out).toHaveLength(2);
    expect(out[0]).toMatchObject({
      kind: "merged_text",
      sessionId: "teammate-alice",
      text: "alice says hi",
    });
    expect(out[1]).toMatchObject({
      kind: "merged_text",
      sessionId: "sess-lead",
      text: "lead replies",
    });
  });

  it("splits when a non-text event interrupts", () => {
    const events: TeamEvent[] = [
      textEvent("teammate-alice", "2026-07-04T10:00:00Z", "before "),
      textEvent("teammate-alice", "2026-07-04T10:00:01Z", "tool"),
      otherEvent("teammate-alice", "2026-07-04T10:00:02Z", "tool_use"),
      textEvent("teammate-alice", "2026-07-04T10:00:03Z", "after"),
    ];
    const out = mergeConsecutiveTexts(events);
    expect(out).toHaveLength(3);
    expect(out[0]).toMatchObject({
      kind: "merged_text",
      text: "before tool",
    });
    expect(out[1]).toMatchObject({ type: "tool_use" });
    expect(out[2]).toMatchObject({
      kind: "merged_text",
      text: "after",
    });
  });

  it("passes through events with no text", () => {
    const events: TeamEvent[] = [
      otherEvent("teammate-alice", "2026-07-04T10:00:00Z", "tool_use"),
      otherEvent("sess-lead", "2026-07-04T10:00:01Z", "tool_result"),
    ];
    const out = mergeConsecutiveTexts(events);
    expect(out).toHaveLength(2);
    expect(out[0]).toBe(events[0]);
    expect(out[1]).toBe(events[1]);
  });

  it("returns empty for empty input", () => {
    expect(mergeConsecutiveTexts([])).toEqual([]);
  });

  it("stamps the merged bubble with the FIRST chunk's ts", () => {
    // The timeline renders newest-first, but for an ascending ts feed
    // the first chunk is the earliest. We want the bubble's ts to
    // reflect when the message started, not when it finished.
    const events: TeamEvent[] = [
      textEvent("teammate-alice", "2026-07-04T10:00:05Z", "first "),
      textEvent("teammate-alice", "2026-07-04T10:00:30Z", "last"),
    ];
    const out = mergeConsecutiveTexts(events);
    expect(out[0]).toMatchObject({ ts: "2026-07-04T10:00:05Z" });
  });
});
