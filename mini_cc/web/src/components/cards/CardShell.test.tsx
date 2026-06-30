import { describe, it, expect, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { CardShell } from "./CardShell";
import { useChat } from "../../lib/store";

describe("CardShell", () => {
  beforeEach(() => {
    useChat.setState({
      runCommand: (cmd: string) => {
        (globalThis as unknown as { __lastCmd?: string }).__lastCmd = cmd;
      },
    });
  });

  it("renders title and icon in the header as a named region", () => {
    render(
      <CardShell title="Teammates" icon="agents" status="ok">
        <div>body</div>
      </CardShell>,
    );
    expect(screen.getByText("Teammates")).toBeInTheDocument();
    expect(
      screen.getByRole("region", { name: /teammates/i }),
    ).toBeInTheDocument();
  });

  it("renders body content", () => {
    render(
      <CardShell title="X" icon={null} status="ok">
        <div>hello-body</div>
      </CardShell>,
    );
    expect(screen.getByText("hello-body")).toBeInTheDocument();
  });

  it("renders error banner when status=error", () => {
    render(
      <CardShell title="X" icon={null} status="error" error_message="boom">
        <div />
      </CardShell>,
    );
    expect(screen.getByText("boom")).toBeInTheDocument();
  });

  it("renders warning status visibly distinct from ok", () => {
    const { container } = render(
      <CardShell title="X" icon={null} status="warning">
        <div />
      </CardShell>,
    );
    expect(container.textContent).toMatch(/warning/i);
  });

  it("renders action buttons in footer", () => {
    render(
      <CardShell
        title="T"
        icon={null}
        status="ok"
        actions={[
          { label: "spawn", command: "/agents spawn", tone: "accent" },
          { label: "refresh", command: "/agents", tone: "default" },
        ]}
      >
        <div />
      </CardShell>,
    );
    expect(
      screen.getByRole("button", { name: /spawn/i }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: /refresh/i }),
    ).toBeInTheDocument();
  });

  it("action button triggers runCommand with the action's command string", async () => {
    const user = userEvent.setup();
    render(
      <CardShell
        title="T"
        icon={null}
        status="ok"
        actions={[
          { label: "spawn", command: "/agents spawn", tone: "accent" },
        ]}
      >
        <div />
      </CardShell>,
    );
    await user.click(screen.getByRole("button", { name: /spawn/i }));
    expect(
      (globalThis as unknown as { __lastCmd?: string }).__lastCmd,
    ).toBe("/agents spawn");
  });
});
