import { describe, it, expect } from "vitest";
import {
  mergeConsecutiveTexts,
  speakerLabel,
  summarizeEvent,
} from "./teamEvent";
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

  it("preserves stream order within a bubble (left-to-right reading)", () => {
    // Regression: when the timeline reversed the activity stream
    // BEFORE merging, each bubble's chunks were joined newest-first
    // and the text read right-to-left. Verify the helper joins chunks
    // in the order they appear in the input array (which the caller
    // guarantees is ascending-by-ts).
    const events: TeamEvent[] = [
      textEvent("teammate-alice", "2026-07-04T10:00:00Z", "Hello "),
      textEvent("teammate-alice", "2026-07-04T10:00:01Z", "world, "),
      textEvent("teammate-alice", "2026-07-04T10:00:02Z", "from "),
      textEvent("teammate-alice", "2026-07-04T10:00:03Z", "alice."),
    ];
    const out = mergeConsecutiveTexts(events);
    expect(out[0]).toMatchObject({ text: "Hello world, from alice." });
  });
});

describe("speakerLabel", () => {
  it("returns the session label for plain text events", () => {
    const e = textEvent("teammate-alice", "2026-07-04T10:00:00Z", "hi");
    expect(speakerLabel(e)).toBe("alice");
  });

  it("returns the lead label for lead-session text events", () => {
    const e = textEvent("sess-lead-1", "2026-07-04T10:00:00Z", "hi");
    expect(speakerLabel(e)).toBe("lead");
  });

  it("uses the `from` field for teammate_message events hosted on the lead session", () => {
    // teammate_message events are emitted into the LEAD session by the
    // teammate — sessionLabel would say "lead", but the actual speaker
    // is the teammate identified by `from`.
    const e = {
      session_id: "sess-lead-1",
      ts: "2026-07-04T10:00:00Z",
      type: "teammate_message",
      from: "alice",
      to: "lead",
      content: "milestone reached",
    } as unknown as TeamEvent;
    expect(speakerLabel(e)).toBe("alice");
  });

  it("falls back to sessionLabel when `from` equals the host", () => {
    // Defensive: a message where `from` is the same as the host session
    // label shouldn't render the name twice. Skip the redundant `from`.
    const e = {
      session_id: "teammate-alice",
      ts: "2026-07-04T10:00:00Z",
      type: "teammate_message",
      from: "alice",
      content: "x",
    } as unknown as TeamEvent;
    expect(speakerLabel(e)).toBe("alice");
  });

  it("falls back to sessionLabel when `from` is missing", () => {
    const e = {
      session_id: "teammate-alice",
      ts: "2026-07-04T10:00:00Z",
      type: "tool_use",
      name: "check_inbox",
    } as unknown as TeamEvent;
    expect(speakerLabel(e)).toBe("alice");
  });
});

describe("summarizeEvent — teammate_message case", () => {
  // Regression: teammate_message events (alice/bob/carl's broadcast)
  // used to fall through to the default case and render as
  // "[teammate_message]" — hiding the actual dialogue content.
  it("renders the message content (not a [teammate_message] placeholder)", () => {
    const e = {
      session_id: "sess-lead-1",
      ts: "2026-07-04T10:00:00Z",
      type: "teammate_message",
      from: "alice",
      to: "lead",
      content: "我刚才编了一个离谱的谎言，说蚊子抬动了老牛",
      msg_type: "performance",
    } as unknown as TeamEvent;
    const summary = summarizeEvent(e);
    expect(summary).not.toBe("[teammate_message]");
    expect(summary).toContain("蚊子抬动了老牛");
  });

  it("truncates very long performance messages", () => {
    const long = "词".repeat(200);
    const e = {
      session_id: "sess-lead-1",
      ts: "2026-07-04T10:00:00Z",
      type: "teammate_message",
      from: "bob",
      to: "lead",
      content: long,
      msg_type: "performance",
    } as unknown as TeamEvent;
    const summary = summarizeEvent(e);
    expect(summary.length).toBeLessThanOrEqual(83); // 80 + ellipsis
    expect(summary).toContain("…");
  });

  it("handles missing content gracefully", () => {
    const e = {
      session_id: "sess-lead-1",
      ts: "2026-07-04T10:00:00Z",
      type: "teammate_message",
      from: "carl",
      to: "lead",
      msg_type: "milestone",
    } as unknown as TeamEvent;
    const summary = summarizeEvent(e);
    // Should still produce something readable, not crash.
    expect(typeof summary).toBe("string");
    expect(summary.length).toBeGreaterThan(0);
  });
});

describe("mergeConsecutiveTexts — hallucinated envelope stripping", () => {
  // Regression: when the LLM emits its own input envelope (e.g.
  // <teammate_messages>...</teammate_messages>) as plain text — usually
  // chunk-by-chunk across many text events — the merger used to join
  // them into one giant bubble showing the raw XML/JSON envelope. The
  // envelope is internal protocol; never meant for human reading.
  it("strips a complete <teammate_messages>...</teammate_messages> envelope", () => {
    const events: TeamEvent[] = [
      textEvent("sess-lead-1", "2026-07-04T10:00:00Z", "<teammate_messages>"),
      textEvent(
        "sess-lead-1",
        "2026-07-04T10:00:01Z",
        '[{"from":"carl","to":"lead"}]',
      ),
      textEvent("sess-lead-1", "2026-07-04T10:00:02Z", "</teammate_messages>"),
    ];
    const out = mergeConsecutiveTexts(events);
    expect(out).toHaveLength(0);
  });

  it("strips an unclosed envelope (LLM often forgets the close tag)", () => {
    const events: TeamEvent[] = [
      textEvent("sess-lead-1", "2026-07-04T10:00:00Z", "<teammate_messages>"),
      textEvent("sess-lead-1", "2026-07-04T10:00:01Z", "garbage payload"),
    ];
    const out = mergeConsecutiveTexts(events);
    expect(out).toHaveLength(0);
  });

  it("drops a bubble even if real text preceded the hallucinated envelope", () => {
    // Conservative rule: any envelope tag in the merged bubble text →
    // drop the whole bubble. We COULD surgically preserve the leading
    // "真实输出" but in practice when the LLM hallucinates an envelope
    // it dominates the bubble and any preceding text is usually
    // half-formed garbage too. Reliable hiding beats surgical rescue.
    const events: TeamEvent[] = [
      textEvent("sess-lead-1", "2026-07-04T10:00:00Z", "真实输出 "),
      textEvent(
        "sess-lead-1",
        "2026-07-04T10:00:01Z",
        "<teammate_messages>[{",
      ),
      textEvent("sess-lead-1", "2026-07-04T10:00:02Z", '"from":"x"}]'),
    ];
    const out = mergeConsecutiveTexts(events);
    expect(out).toHaveLength(0);
  });

  it("also strips <inbox> envelope hallucinations", () => {
    const events: TeamEvent[] = [
      textEvent("sess-lead-1", "2026-07-04T10:00:00Z", "<inbox>"),
      textEvent("sess-lead-1", "2026-07-04T10:00:01Z", "raw json garbage"),
      textEvent("sess-lead-1", "2026-07-04T10:00:02Z", "</inbox>"),
    ];
    const out = mergeConsecutiveTexts(events);
    expect(out).toHaveLength(0);
  });

  it("carries envelope state across bubbles split by a real event", () => {
    // Regression: LLM emits envelope-open, then a real teammate_message
    // event interrupts (flushing the current bubble), then the LLM
    // continues emitting envelope-close + JSON fragments as a NEW
    // bubble. The post-interrupt continuation used to leak through as
    // a stand-alone JSON-garbage bubble because state didn't carry.
    const events: TeamEvent[] = [
      textEvent("sess-lead-1", "2026-07-04T10:00:00Z", "<teammate_messages>"),
      textEvent("sess-lead-1", "2026-07-04T10:00:01Z", '[{"from":"x"}'),
      otherEvent("sess-lead-1", "2026-07-04T10:00:02Z", "teammate_message"),
      textEvent("sess-lead-1", "2026-07-04T10:00:03Z", ',"content":"y"}]'),
      textEvent("sess-lead-1", "2026-07-04T10:00:04Z", "</teammate_messages>"),
    ];
    const out = mergeConsecutiveTexts(events);
    // Expected: only the teammate_message event survives; no junk
    // text bubble from the envelope's tail.
    const textBubbles = out.filter((r) => r.kind === "merged_text");
    expect(textBubbles).toHaveLength(0);
    expect(out.some((r) => r.kind !== "merged_text")).toBe(true);
  });

  it("strips hallucinated <function_calls> tool-call XML", () => {
    // The LLM sometimes emits Anthropic tool-call XML as plain text
    // (typed at the keyboard) instead of via the structured tool_use
    // event. The result is a bubble showing raw syntax like:
    //   <function_calls> <invoke name="todo_write"> ...
    // which is never meant for human reading.
    const events: TeamEvent[] = [
      textEvent("sess-lead-1", "2026-07-04T10:00:00Z", "<function_calls>"),
      textEvent(
        "sess-lead-1",
        "2026-07-04T10:00:01Z",
        ' <invoke name="todo_write">',
      ),
      textEvent(
        "sess-lead-1",
        "2026-07-04T10:00:02Z",
        ' <parameter name="todos">[...]</parameter> </invoke> </function_calls>',
      ),
    ];
    const out = mergeConsecutiveTexts(events);
    expect(out).toHaveLength(0);
  });

  it("strips the cross-bubble tail of a hallucinated tool-call", () => {
    // Same cross-bubble regression as the teammate_messages case above,
    // but for tool-call XML. The LLM emits the opening, a real event
    // interrupts, and the post-interrupt continuation contains
    // </parameter> </invoke> </function_calls> close tags with no open.
    const events: TeamEvent[] = [
      textEvent("sess-lead-1", "2026-07-04T10:00:00Z", "<function_calls>"),
      otherEvent("sess-lead-1", "2026-07-04T10:00:01Z", "tool_use"),
      textEvent(
        "sess-lead-1",
        "2026-07-04T10:00:02Z",
        '现在，我将按照上面的要求进行更新。 </parameter> </invoke> </function_calls>',
      ),
    ];
    const out = mergeConsecutiveTexts(events);
    const textBubbles = out.filter((r) => r.kind === "merged_text");
    expect(textBubbles).toHaveLength(0);
  });
});
