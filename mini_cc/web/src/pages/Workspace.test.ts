import { describe, expect, it } from "vitest";
import { formatLeadNudgedNotice } from "./Workspace";

// Phase I.C.5: the lead_nudged notice formatter. Pure function — no
// store, no SSE. The output is one line of gray text rendered through
// the existing ChatMessage.notices slot. These tests pin the wording
// shape so a later refactor can't silently drift the spec's "Alice
// reported a milestone → lead is responding..." phrasing.

describe("formatLeadNudgedNotice", () => {
  it("formats a single milestone", () => {
    expect(
      formatLeadNudgedNotice([{ from: "Alice", kind: "milestone" }]),
    ).toBe("Alice reported a milestone → lead is responding...");
  });

  it("formats a blocker with the right verb", () => {
    expect(
      formatLeadNudgedNotice([{ from: "Bob", kind: "blocker" }]),
    ).toBe("Bob hit a blocker → lead is responding...");
  });

  it("formats a result with the right verb", () => {
    expect(
      formatLeadNudgedNotice([{ from: "Carl", kind: "result" }]),
    ).toBe("Carl reported a result → lead is responding...");
  });

  it("falls back to a generic verb for unknown kinds", () => {
    expect(
      formatLeadNudgedNotice([{ from: "Dan", kind: "ping" }]),
    ).toBe("Dan reported an update → lead is responding...");
  });

  it("joins multiple teammates on one line", () => {
    expect(
      formatLeadNudgedNotice([
        { from: "Alice", kind: "milestone" },
        { from: "Bob", kind: "blocker" },
      ]),
    ).toBe(
      "Alice reported a milestone, Bob hit a blocker → lead is responding...",
    );
  });

  it("handles an empty list defensively", () => {
    expect(formatLeadNudgedNotice([])).toBe(
      " → lead is responding...",
    );
  });
});
