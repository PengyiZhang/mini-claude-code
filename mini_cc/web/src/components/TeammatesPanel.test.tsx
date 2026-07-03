import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import {
  render,
  screen,
  fireEvent,
  cleanup,
  waitFor,
} from "@testing-library/react";
import TeammatesPanel from "./TeammatesPanel";
import { useTeamActivity } from "../lib/teamActivity";
import { useAuth, useChat } from "../lib/store";
import { runCommandForCard } from "../lib/commands";
import type { CardEvent } from "../lib/types";

// --- Mocks ----------------------------------------------------------------

vi.mock("../lib/commands", () => ({
  runCommandForCard: vi.fn(),
}));

vi.mock("../lib/store", () => ({
  useAuth: vi.fn(),
  useChat: vi.fn(),
}));

const runCommandForCardMock = runCommandForCard as unknown as ReturnType<
  typeof vi.fn
>;
const useAuthMock = useAuth as unknown as ReturnType<typeof vi.fn>;
const useChatMock = useChat as unknown as ReturnType<typeof vi.fn>;

const profile = { label: "t", tenantId: "t", apiKey: "k", baseUrl: "http://x" };

function rosterCard(): CardEvent {
  return {
    type: "card",
    ts: "2026-07-04T10:00:00Z",
    title: "Agents",
    payload: {
      items: [
        {
          id: "alice",
          title: "alice",
          subtitle: "vision-language 研究员",
          meta: "0 in",
          badges: [{ text: "alive", tone: "ok" }],
          menu: [],
        },
        {
          id: "bob",
          title: "bob",
          subtitle: "audio-language 研究员",
          meta: "1 in",
          badges: [{ text: "stopped", tone: "muted" }],
          menu: [],
        },
      ],
    },
  } as unknown as CardEvent;
}

beforeEach(() => {
  vi.clearAllMocks();
  useAuthMock.mockReturnValue(profile);
  useChatMock.mockReturnValue(() => {});
  // Default: every /agents call returns the roster so the mount-time tick
  // renders rows. Tests that need different behavior can override.
  runCommandForCardMock.mockResolvedValue(rosterCard());
  useTeamActivity.setState({
    perProjectActivity: {},
    since: {},
    busy: {},
    error: {},
  });
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

// Helper: wait until the roster (alice/bob rows) has rendered after the
// mount-time tick. The component awaits runCommandForCard then setState,
// so we must flush microtasks and wait for the re-render.
async function settleRoster() {
  await waitFor(() => {
    expect(screen.getByText(/@alice/)).toBeInTheDocument();
  });
}

describe("TeammatesPanel", () => {
  it("renders roster rows from /agents card", async () => {
    render(<TeammatesPanel pid="p1" sid="s1" />);
    await settleRoster();
    expect(screen.getByText(/@alice/)).toBeInTheDocument();
    expect(screen.getByText(/@bob/)).toBeInTheDocument();
  });

  it("expanded row shows recent activity events from the store", async () => {
    useTeamActivity.setState({
      perProjectActivity: {
        p1: [
          {
            session_id: "teammate-alice",
            ts: "2026-07-04T10:00:00Z",
            type: "tool_use",
            name: "bash",
            input: { command: "ls -la /tmp" },
          },
          {
            session_id: "teammate-alice",
            ts: "2026-07-04T10:00:01Z",
            type: "tool_result",
            tool_use_id: "x",
            content: "file1\nfile2\nfile3",
          },
          {
            session_id: "teammate-alice",
            ts: "2026-07-04T10:00:02Z",
            type: "send_message",
            to: "bob",
            message: "did you see the new benchmark?",
          },
          {
            session_id: "teammate-alice",
            ts: "2026-07-04T10:00:03Z",
            type: "text",
            text: "thinking about the results",
          },
        ],
      },
    });

    render(<TeammatesPanel pid="p1" sid="s1" />);
    await settleRoster();

    // Find the alice row header button (starts with "@alice" text). Click to expand.
    const aliceHeader = screen.getByText(/@alice/).closest("button")!;
    fireEvent.click(aliceHeader);

    // The most-recent event (text, ts 10:00:03) should appear at the top.
    expect(screen.getByText(/recent activity/i)).toBeInTheDocument();
    expect(screen.getByText(/thinking about the results/)).toBeInTheDocument();
    // send_message rendering: arrow + to
    expect(screen.getByText(/→ bob/)).toBeInTheDocument();
    // tool_use: name shown
    expect(screen.getByText(/^bash/, { exact: false })).toBeInTheDocument();
  });

  it("empty activity shows placeholder", async () => {
    // No events for alice in store.
    render(<TeammatesPanel pid="p1" sid="s1" />);
    await settleRoster();

    fireEvent.click(screen.getByText(/@alice/).closest("button")!);
    expect(screen.getByText(/\(no recent activity\)/i)).toBeInTheDocument();
  });

  it("polls team activity on mount", async () => {
    const pollSpy = vi
      .spyOn(useTeamActivity.getState(), "poll")
      .mockResolvedValue(undefined);

    render(<TeammatesPanel pid="p1" sid="s1" />);
    // The mount tick fires poll() inside an effect; wrap in waitFor
    // because React 18 may batch the effect commit across microtasks
    // and a synchronous assertion races the scheduler.
    await waitFor(() => {
      expect(pollSpy).toHaveBeenCalledWith(profile, "p1");
    });

    pollSpy.mockRestore();
  });

  it("limits recent activity to the last 10 events", async () => {
    const events = Array.from({ length: 15 }, (_, i) => ({
      session_id: "teammate-alice",
      ts: `2026-07-04T10:00:${String(i).padStart(2, "0")}Z`,
      type: "text",
      text: `msg-${i}`,
    }));
    useTeamActivity.setState({ perProjectActivity: { p1: events } });

    render(<TeammatesPanel pid="p1" sid="s1" />);
    await settleRoster();
    fireEvent.click(screen.getByText(/@alice/).closest("button")!);

    // Last 10 = msg-5 .. msg-14
    expect(screen.getByText(/msg-14/)).toBeInTheDocument();
    expect(screen.getByText(/msg-5/)).toBeInTheDocument();
    expect(screen.queryByText(/msg-4/)).not.toBeInTheDocument();
  });

  it("does not show activity for other teammates in this row", async () => {
    useTeamActivity.setState({
      perProjectActivity: {
        p1: [
          {
            session_id: "teammate-bob",
            ts: "2026-07-04T10:00:00Z",
            type: "text",
            text: "bob-only-secret",
          },
        ],
      },
    });

    render(<TeammatesPanel pid="p1" sid="s1" />);
    await settleRoster();
    fireEvent.click(screen.getByText(/@alice/).closest("button")!);

    expect(screen.queryByText(/bob-only-secret/)).not.toBeInTheDocument();
  });
});
