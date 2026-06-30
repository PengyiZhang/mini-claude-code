import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { CardKeyValue } from "./CardKeyValue";
import type { CardKeyValuePayload } from "../../lib/types";

function renderPairs(pairs: CardKeyValuePayload["pairs"]) {
  return render(<CardKeyValue payload={{ pairs }} />);
}

describe("CardKeyValue", () => {
  it("renders pairs as label/value rows", () => {
    renderPairs([
      { k: "Model", v: "glm-4.7", mono: false, sensitive: false, badge: null },
      { k: "Backend", v: "anthropic", mono: false, sensitive: false, badge: null },
    ]);
    expect(screen.getByText("Model")).toBeInTheDocument();
    expect(screen.getByText("glm-4.7")).toBeInTheDocument();
    expect(screen.getByText("Backend")).toBeInTheDocument();
  });

  it("wraps mono values in <code>", () => {
    const { container } = renderPairs([
      { k: "Model", v: "glm-4.7", mono: true, sensitive: false, badge: null },
    ]);
    // We don't pin exact classes; just assert the value is rendered
    // inside a <code> element.
    const code = container.querySelector("code");
    expect(code?.textContent).toBe("glm-4.7");
  });

  it("masks sensitive values until reveal is clicked", () => {
    renderPairs([
      {
        k: "API key",
        v: "sk-a••••7890",
        mono: true,
        sensitive: true,
        badge: null,
      },
    ]);
    // Sensitive values render as dots by default. The reveal affordance
    // is a button labeled with an eye icon or "show" — pick a stable
    // accessible name.
    expect(screen.queryByText("sk-a••••7890")).not.toBeInTheDocument();
    // The masked form is shown as ••••• (we don't pin exact count).
    expect(screen.getByText(/^•+$/)).toBeInTheDocument();
  });

  it("reveals the real value when the show button is clicked", async () => {
    const user = userEvent.setup();
    renderPairs([
      {
        k: "API key",
        v: "sk-a••••7890",
        mono: true,
        sensitive: true,
        badge: null,
      },
    ]);
    const reveal = screen.getByRole("button", { name: /show|reveal|eye/i });
    await user.click(reveal);
    expect(screen.getByText("sk-a••••7890")).toBeInTheDocument();
  });

  it("hides the value again when toggled a second time", async () => {
    const user = userEvent.setup();
    renderPairs([
      {
        k: "API key",
        v: "sk-a••••7890",
        mono: true,
        sensitive: true,
        badge: null,
      },
    ]);
    const reveal = screen.getByRole("button", { name: /show|reveal|eye/i });
    await user.click(reveal);
    await user.click(reveal);
    expect(screen.queryByText("sk-a••••7890")).not.toBeInTheDocument();
  });

  it("renders empty state when pairs is empty", () => {
    renderPairs([]);
    // The component's own empty hint — keep it stable.
    expect(screen.getByText(/no entries/i)).toBeInTheDocument();
  });
});
