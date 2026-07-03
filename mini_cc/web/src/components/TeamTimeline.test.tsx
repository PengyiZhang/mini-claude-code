import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import {
  render,
  screen,
  fireEvent,
  cleanup,
} from "@testing-library/react";
import TeamTimeline from "./TeamTimeline";
import { useTeamActivity } from "../lib/teamActivity";

// --- Mocks ----------------------------------------------------------------
// We don't need store or commands here — TeamTimeline is a pure render
// of the activity store, so we only mock the store itself.

beforeEach(() => {
  useTeamActivity.setState({
    perProjectActivity: {},
    since: {},
    busy: {},
    error: {},
  });
});

afterEach(() => {
  cleanup();
});

// Build a TeamEvent with sensible defaults. Tests override the bits
// they care about.
function ev(overrides: Record<string, unknown>) {
  return {
    session_id: "sess-lead",
    ts: "2026-07-04T10:00:00Z",
    type: "text",
    text: "hello",
    ...overrides,
  };
}

describe("TeamTimeline", () => {
  it("renders events from useTeamActivity", () => {
    useTeamActivity.setState({
      perProjectActivity: {
        p1: [
          ev({
            session_id: "teammate-alice",
            ts: "2026-07-04T10:00:00Z",
            text: "alice did a thing",
          }),
          ev({
            session_id: "sess-lead-1",
            ts: "2026-07-04T10:00:01Z",
            text: "lead responded",
          }),
        ],
      },
    });

    render(<TeamTimeline pid="p1" sid="sess-lead-1" setSid={() => {}} />);

    expect(screen.getByText(/alice did a thing/)).toBeInTheDocument();
    expect(screen.getByText(/lead responded/)).toBeInTheDocument();
    // Session badges derived from session_id appear in both the inline
    // filter and the event rows — assert on the filter labels (which
    // are exact text nodes) to confirm both alice and lead are present.
    expect(screen.getAllByText("alice").length).toBeGreaterThan(0);
    expect(screen.getAllByText("lead").length).toBeGreaterThan(0);
  });

  it("filters by selected teammate", () => {
    useTeamActivity.setState({
      perProjectActivity: {
        p1: [
          ev({
            session_id: "teammate-alice",
            ts: "2026-07-04T10:00:00Z",
            text: "alice event",
          }),
          ev({
            session_id: "teammate-bob",
            ts: "2026-07-04T10:00:01Z",
            text: "bob event",
          }),
          ev({
            session_id: "sess-lead-1",
            ts: "2026-07-04T10:00:02Z",
            text: "lead event",
          }),
        ],
      },
    });

    render(<TeamTimeline pid="p1" sid="sess-lead-1" setSid={() => {}} />);

    // Initially all visible
    expect(screen.getByText(/alice event/)).toBeInTheDocument();
    expect(screen.getByText(/bob event/)).toBeInTheDocument();
    expect(screen.getByText(/lead event/)).toBeInTheDocument();

    // Uncheck alice in the sidebar filter
    const aliceCheckbox = screen
      .getByLabelText(/alice/i)
      .closest("label")!
      .querySelector('input[type="checkbox"]')!;
    fireEvent.click(aliceCheckbox);

    expect(screen.queryByText(/alice event/)).not.toBeInTheDocument();
    expect(screen.getByText(/bob event/)).toBeInTheDocument();
    expect(screen.getByText(/lead event/)).toBeInTheDocument();
  });

  it("clicking lead event calls setSid", () => {
    const setSid = vi.fn();
    useTeamActivity.setState({
      perProjectActivity: {
        p1: [
          ev({
            session_id: "sess-lead-1",
            ts: "2026-07-04T10:00:00Z",
            text: "lead turn",
          }),
        ],
      },
    });

    render(<TeamTimeline pid="p1" sid="other-sess" setSid={setSid} />);

    fireEvent.click(screen.getByText(/lead turn/));
    expect(setSid).toHaveBeenCalledWith("sess-lead-1");
  });

  it("clicking teammate event does not call setSid", () => {
    const setSid = vi.fn();
    useTeamActivity.setState({
      perProjectActivity: {
        p1: [
          ev({
            session_id: "teammate-alice",
            ts: "2026-07-04T10:00:00Z",
            text: "alice turn",
          }),
        ],
      },
    });

    render(<TeamTimeline pid="p1" sid="sess-lead-1" setSid={setSid} />);

    fireEvent.click(screen.getByText(/alice turn/));
    expect(setSid).not.toHaveBeenCalled();
  });

  it("empty state shows placeholder", () => {
    render(<TeamTimeline pid="p1" sid="sess-lead-1" setSid={() => {}} />);
    expect(
      screen.getByText(/no team activity yet/i),
    ).toBeInTheDocument();
  });
});
