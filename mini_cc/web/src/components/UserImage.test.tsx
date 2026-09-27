import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, cleanup } from "@testing-library/react";

// Per-test override target. The mock factory reads this at call time.
let mockReturn: { url: string | null; error: Error | null } = {
  url: null,
  error: null,
};

vi.mock("../lib/useAssetUrl", () => ({
  useAssetUrl: () => mockReturn,
}));

// Import AFTER vi.mock so the module sees the factory.
import UserImage from "./UserImage";
import type { TenantProfile } from "../lib/types";

const profile: TenantProfile = {
  label: "t",
  tenantId: "t",
  apiKey: "k",
  baseUrl: "http://x",
};

beforeEach(() => {
  mockReturn = { url: null, error: null };
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("UserImage", () => {
  it("renders a loading skeleton sized by the caller's className while in flight", () => {
    mockReturn = { url: null, error: null };
    render(
      <UserImage profile={profile} pid="p" sid="s" assetId="abc"
                 className="w-12 h-12" />,
    );
    const skeleton = screen.getByLabelText("loading image");
    // Caller's className applies to the placeholder so the chip doesn't
    // jump dimensions when the real image lands.
    expect(skeleton.className).toContain("w-12");
    expect(skeleton.className).toContain("h-12");
    expect(document.querySelector("img")).toBeNull();
  });

  it("renders the <img> with the blob url and caller-supplied className once loaded", () => {
    mockReturn = { url: "blob:abc", error: null };
    render(
      <UserImage profile={profile} pid="p" sid="s" assetId="abc"
                 className="w-12 h-12 object-cover rounded" />,
    );
    const img = document.querySelector("img");
    expect(img).not.toBeNull();
    expect(img!).toHaveAttribute("src", "blob:abc");
    expect(img!).toHaveAttribute("class", "w-12 h-12 object-cover rounded");
  });

  it("falls back to an error message when the load failed", () => {
    mockReturn = { url: null, error: new Error("HTTP 401") };
    render(
      <UserImage profile={profile} pid="p" sid="s" assetId="abc" />,
    );
    expect(screen.getByText(/\[image: failed to load\]/)).toBeInTheDocument();
    expect(document.querySelector("img")).toBeNull();
  });
});
