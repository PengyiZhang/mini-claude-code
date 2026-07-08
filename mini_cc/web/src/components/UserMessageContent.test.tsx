import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, cleanup } from "@testing-library/react";
import UserMessageContent from "./UserMessageContent";
import type { TenantProfile } from "../lib/types";

// Mock useAssetUrl so the render test doesn't depend on fetch / blob
// plumbing (those are exercised in useAssetUrl.test.ts). Per-asset-id
// behavior: return a deterministic blob URL so we can assert on it.
vi.mock("../lib/useAssetUrl", () => ({
  useAssetUrl: vi.fn((_profile, _pid, _sid, assetId: string | null) => ({
    url: assetId ? `blob:fake-${assetId}` : null,
    error: null,
  })),
}));

const profile: TenantProfile = {
  label: "t",
  tenantId: "tenant1",
  apiKey: "k",
  baseUrl: "http://x",
};

const pid = "p1";
const sid = "s1";

beforeEach(() => {
  vi.clearAllMocks();
});

afterEach(() => {
  cleanup();
});

describe("UserMessageContent", () => {
  it("renders just text when no assets are provided", () => {
    render(
      <UserMessageContent
        text="hello world"
        assets={undefined}
        profile={profile}
        pid={pid}
        sid={sid}
      />,
    );
    expect(screen.getByText("hello world")).toBeInTheDocument();
    expect(document.querySelectorAll("img")).toHaveLength(0);
  });

  it("renders text + an <img> per asset, with src=authenticated blob url", () => {
    render(
      <UserMessageContent
        text="what is this?"
        assets={[
          { asset_id: "abc123", media_type: "image/png" },
          { asset_id: "def456", media_type: "image/jpeg" },
        ]}
        profile={profile}
        pid={pid}
        sid={sid}
      />,
    );
    expect(screen.getByText("what is this?")).toBeInTheDocument();
    const imgs = Array.from(document.querySelectorAll("img"));
    expect(imgs).toHaveLength(2);
    // Each <img src> comes from useAssetUrl's mocked return value — the
    // raw assetUrl(...) string must NOT appear here, or browsers would
    // hit the unauthenticated path and 401 (Bug B).
    expect(imgs[0]).toHaveAttribute("src", "blob:fake-abc123");
    expect(imgs[1]).toHaveAttribute("src", "blob:fake-def456");
    expect(imgs[0].getAttribute("src")).not.toContain("/assets/");
  });

  it("renders images even when text is empty (pure-image turn)", () => {
    render(
      <UserMessageContent
        text=""
        assets={[
          { asset_id: "onlyimg", media_type: "image/png" },
        ]}
        profile={profile}
        pid={pid}
        sid={sid}
      />,
    );
    const img = document.querySelector("img");
    expect(img).not.toBeNull();
    expect(img!).toHaveAttribute("src", "blob:fake-onlyimg");
  });
});
