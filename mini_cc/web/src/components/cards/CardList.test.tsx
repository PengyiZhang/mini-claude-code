import { describe, it, expect, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { CardList } from "./CardList";
import { useChat } from "../../lib/store";
import type { CardListPayload, CardListItem } from "../../lib/types";

function item(overrides: Partial<CardListItem> = {}): CardListItem {
  return {
    id: "i1",
    title: "Item One",
    subtitle: null,
    icon: null,
    badges: [],
    meta: null,
    expandable_command: null,
    menu: [],
    ...overrides,
  };
}

function runList(payload: Partial<CardListPayload> = {}) {
  return render(
    <CardList
      payload={{
        items: [item()],
        empty_hint: null,
        summary: null,
        group_by: null,
        ...payload,
      } as CardListPayload}
    />,
  );
}

describe("CardList", () => {
  beforeEach(() => {
    useChat.setState({
      runCommand: (cmd: string) => {
        (globalThis as unknown as { __lastCmd?: string }).__lastCmd = cmd;
      },
    });
  });

  it("renders items in order with title + subtitle + badges + meta", () => {
    render(
      <CardList
        payload={{
          items: [
            item({
              id: "a",
              title: "Alice",
              subtitle: "researcher",
              badges: [{ text: "alive", tone: "ok" }],
              meta: "4m",
            }),
            item({
              id: "b",
              title: "Bob",
              subtitle: "coder",
              badges: [{ text: "stopped", tone: "default" }],
              meta: "1h",
            }),
          ],
          empty_hint: null,
          summary: null,
          group_by: null,
        } as CardListPayload}
      />,
    );
    expect(screen.getByText("Alice")).toBeInTheDocument();
    expect(screen.getByText("researcher")).toBeInTheDocument();
    expect(screen.getByText("alive")).toBeInTheDocument();
    expect(screen.getByText("4m")).toBeInTheDocument();
    expect(screen.getByText("Bob")).toBeInTheDocument();
    expect(screen.getByText("coder")).toBeInTheDocument();
  });

  it("shows empty_hint when items is empty", () => {
    render(
      <CardList
        payload={{
          items: [],
          empty_hint: "no teammates yet",
          summary: null,
          group_by: null,
        } as CardListPayload}
      />,
    );
    expect(screen.getByText(/no teammates yet/i)).toBeInTheDocument();
  });

  it("renders summary footer when present", () => {
    runList({ summary: "2 alive · 1 stopped" });
    expect(screen.getByText(/2 alive · 1 stopped/)).toBeInTheDocument();
  });

  it("does not render summary footer when absent", () => {
    const { container } = runList({ summary: null });
    expect(container.textContent).not.toMatch(/alive · stopped/);
  });

  it("clicking a row with expandable_command triggers runCommand", async () => {
    const user = userEvent.setup();
    render(
      <CardList
        payload={{
          items: [
            item({
              id: "alice",
              title: "Alice",
              expandable_command: "/agents inbox alice",
            }),
          ],
          empty_hint: null,
          summary: null,
          group_by: null,
        } as CardListPayload}
      />,
    );
    await user.click(screen.getByRole("button", { name: /alice/i }));
    expect(
      (globalThis as unknown as { __lastCmd?: string }).__lastCmd,
    ).toBe("/agents inbox alice");
  });

  it("renders per-row menu actions and dispatches their commands", async () => {
    const user = userEvent.setup();
    render(
      <CardList
        payload={{
          items: [
            item({
              id: "alice",
              title: "Alice",
              menu: [
                { label: "stop", command: "/agents stop alice", tone: "default" },
              ],
            }),
          ],
          empty_hint: null,
          summary: null,
          group_by: null,
        } as CardListPayload}
      />,
    );
    await user.click(screen.getByRole("button", { name: /stop/i }));
    expect(
      (globalThis as unknown as { __lastCmd?: string }).__lastCmd,
    ).toBe("/agents stop alice");
  });

  it("row without expandable_command is not a button", () => {
    render(
      <CardList
        payload={{
          items: [item({ id: "x", title: "Plain", expandable_command: null })],
          empty_hint: null,
          summary: null,
          group_by: null,
        } as CardListPayload}
      />,
    );
    expect(screen.queryByRole("button", { name: /plain/i })).toBeNull();
  });
});
