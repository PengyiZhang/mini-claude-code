import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, cleanup, fireEvent } from "@testing-library/react";
import type { ReactNode } from "react";

// Mock the auth/chat stores so MessageBubble can read the active profile
// without dragging in zustand + localStorage plumbing.
vi.mock("../lib/store", () => ({
  useAuth: vi.fn(() => ({
    label: "t",
    tenantId: "tenant1",
    apiKey: "k",
    baseUrl: "http://x",
  })),
  useChat: vi.fn(() => vi.fn()),
  useSessionNav: vi.fn(() => vi.fn()),
}));

// Mock MarkdownRenderer to keep the test isolated from the markdown lib.
vi.mock("./MarkdownRenderer", () => ({
  MarkdownRenderer: ({ content }: { content: string }) => (
    <div data-testid="md">{content}</div>
  ),
}));

// Mock useAssetUrl so the rendered <img> has a deterministic src.
vi.mock("../lib/useAssetUrl", () => ({
  useAssetUrl: vi.fn(
    (_p: unknown, _pid: string, _sid: string, assetId: string | null) => ({
      url: assetId ? `blob:fake-${assetId}` : null,
      error: null,
    }),
  ),
}));

// Import AFTER vi.mock so the module sees the factories.
import MessageBubble from "./MessageBubble";
import type { ChatMessage } from "../lib/store";

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

// Helper: build a user-role ChatMessage with the same synthetic __key
// the Workspace renderer attaches via MessageBubbleWithKey. Pinned by
// Bug 1 fix: the chatKey separator is "::" (Workspace.tsx), but an
// earlier version of MessageBubble split on "/" — producing
// pid="<pid>::<sid>" and sid=undefined, which silently fell back to
// the text-only render path so the user's image never appeared.
function userMsgWithKey(
  msg: ChatMessage,
  chatKey: string,
): ChatMessage & { __key: string } {
  return { ...msg, __key: chatKey } as ChatMessage & { __key: string };
}

describe("MessageBubble user-turn image rendering", () => {
  it("renders <img> for an asset-bearing user message keyed with the canonical '::' chatKey", () => {
    const msg = userMsgWithKey(
      {
        role: "user",
        text: "see this?",
        assets: [{ asset_id: "abc", media_type: "image/png" }],
      },
      "my-proj::sess-1",
    );
    render(<MessageBubble msg={msg} />);
    const img = document.querySelector("img");
    expect(img).not.toBeNull();
    expect(img!).toHaveAttribute("src", "blob:fake-abc");
  });
});

describe("MessageBubble thinking disclosure", () => {
  it("renders a collapsed thinking disclosure when msg.thinking is set", () => {
    const msg = {
      ...userMsgWithKey(
        {
          role: "assistant",
          text: "answer",
          streaming: false,
          activities: [],
          cards: [],
          notices: [],
        } as ChatMessage,
        "p::s",
      ),
      thinking: "step 1: reason",
    } as ChatMessage & { __key: string };
    render(<MessageBubble msg={msg} />);
    // Header chip is visible.
    const toggle = screen.getByRole("button", { name: /thinking/i });
    expect(toggle).toBeInTheDocument();
    // Collapsed by default: only the answer markdown renders; the
    // disclosure body stays hidden until expanded.
    expect(screen.getAllByTestId("md")).toHaveLength(1);
    expect(screen.getAllByTestId("md")[0].textContent).toBe("answer");
  });

  it("expands the thinking body on click", () => {
    const msg = {
      ...userMsgWithKey(
        {
          role: "assistant",
          text: "answer",
          streaming: false,
          activities: [],
          cards: [],
          notices: [],
        } as ChatMessage,
        "p::s",
      ),
      thinking: "private reasoning",
    } as ChatMessage & { __key: string };
    render(<MessageBubble msg={msg} />);
    const toggle = screen.getByRole("button", { name: /thinking/i });
    fireEvent.click(toggle);
    // After expand, the markdown testid appears in 2 places
    // (disclosure body + answer body).
    expect(screen.getAllByTestId("md").length).toBe(2);
  });

  it("does not render a thinking disclosure when msg.thinking is absent", () => {
    const msg = userMsgWithKey(
      {
        role: "assistant",
        text: "answer",
        streaming: false,
        activities: [],
        cards: [],
        notices: [],
      } as ChatMessage,
      "p::s",
    );
    render(<MessageBubble msg={msg} />);
    expect(screen.queryByRole("button", { name: /thinking/i })).toBeNull();
  });
});
