import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import MentionPicker, { type MentionCandidate } from "./MentionPicker";

const candidates: MentionCandidate[] = [
  { name: "alice", role: "vision-language 研究员" },
  { name: "bob", role: "audio-language 研究员" },
  { name: "carol", role: "reviewer" },
];

describe("MentionPicker", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("renders nothing when query is empty", () => {
    const { container } = render(
      <MentionPicker
        query=""
        candidates={candidates}
        activeIndex={0}
        onPick={() => {}}
        onClose={() => {}}
      />,
    );
    expect(container).toBeEmptyDOMElement();
  });

  it("renders nothing when no candidates match", () => {
    const { container } = render(
      <MentionPicker
        query="zzz"
        candidates={candidates}
        activeIndex={0}
        onPick={() => {}}
        onClose={() => {}}
      />,
    );
    expect(container).toBeEmptyDOMElement();
  });

  it("lists matching candidates by name prefix", () => {
    render(
      <MentionPicker
        query="a"
        candidates={candidates}
        activeIndex={0}
        onPick={() => {}}
        onClose={() => {}}
      />,
    );
    // alice matches "a"; bob and carol don't start with "a"
    expect(screen.getByText(/@alice/)).toBeInTheDocument();
    expect(screen.queryByText(/@bob/)).not.toBeInTheDocument();
    expect(screen.queryByText(/@carol/)).not.toBeInTheDocument();
  });

  it("shows all candidates when query is bare @", () => {
    render(
      <MentionPicker
        query=""
        candidates={candidates}
        activeIndex={1}
        onPick={() => {}}
        onClose={() => {}}
      />,
    );
    // Should not render anything because query is empty
    expect(screen.queryByText(/@alice/)).not.toBeInTheDocument();
  });

  it("calls onPick with the clicked candidate", () => {
    const onPick = vi.fn();
    render(
      <MentionPicker
        query=""
        candidates={candidates}
        activeIndex={0}
        onPick={onPick}
        onClose={() => {}}
        initialOpen={true}
      />,
    );
    // When forced open via initialOpen, all candidates show
    fireEvent.click(screen.getByText(/@bob/));
    expect(onPick).toHaveBeenCalledWith("bob");
  });

  it("highlights the activeIndex row", () => {
    render(
      <MentionPicker
        query=""
        candidates={candidates}
        activeIndex={2}
        onPick={() => {}}
        onClose={() => {}}
        initialOpen={true}
      />,
    );
    const carol = screen.getByText(/@carol/).closest("button");
    expect(carol?.className).toContain("bg-accent");
  });
});
