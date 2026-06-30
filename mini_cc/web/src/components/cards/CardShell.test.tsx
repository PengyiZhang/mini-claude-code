import { describe, it, expect, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { CardShell } from "./CardShell";
import { useChat } from "../../lib/store";

describe("CardShell", () => {
  beforeEach(() => {
    (globalThis as unknown as { __lastCmd?: string }).__lastCmd = undefined;
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

  it("non-spawn action triggers runCommand with the action's command", async () => {
    const user = userEvent.setup();
    render(
      <CardShell
        title="T"
        icon={null}
        status="ok"
        actions={[
          { label: "refresh", command: "/agents", tone: "default" },
        ]}
      >
        <div />
      </CardShell>,
    );
    await user.click(screen.getByRole("button", { name: /refresh/i }));
    expect(
      (globalThis as unknown as { __lastCmd?: string }).__lastCmd,
    ).toBe("/agents");
  });

  it("spawn action opens inline form instead of firing the raw command", async () => {
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
    // Raw command would error; the form should open instead.
    expect(
      (globalThis as unknown as { __lastCmd?: string }).__lastCmd,
    ).toBeUndefined();
    // The form has a name input and a prompt textarea.
    expect(screen.getByPlaceholderText("alice")).toBeInTheDocument();
    expect(screen.getByPlaceholderText(/Find the latest test results/)).toBeInTheDocument();
  });

  it("spawn form builds slash command on submit and closes", async () => {
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
    await user.type(screen.getByPlaceholderText("alice"), "alice");
    await user.type(screen.getByPlaceholderText("researcher"), "researcher");
    await user.type(
      screen.getByPlaceholderText(/Find the latest test results/),
      "summarize the latest test run",
    );
    await user.click(screen.getByRole("button", { name: /spawn teammate/i }));
    expect(
      (globalThis as unknown as { __lastCmd?: string }).__lastCmd,
    ).toBe('/agents spawn alice researcher --prompt "summarize the latest test run"');
    // Form closed after submit.
    expect(screen.queryByPlaceholderText("alice")).not.toBeInTheDocument();
  });

  it("spawn form requires name, role, and prompt before submit", async () => {
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
    // Submit without filling anything.
    await user.click(screen.getByRole("button", { name: /spawn teammate/i }));
    expect(
      (globalThis as unknown as { __lastCmd?: string }).__lastCmd,
    ).toBeUndefined();
    expect(screen.getByText(/all required/i)).toBeInTheDocument();
  });
});
