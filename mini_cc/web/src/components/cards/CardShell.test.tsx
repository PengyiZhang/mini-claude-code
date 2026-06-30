import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import { CardShell } from "./CardShell";

describe("CardShell", () => {
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
});
