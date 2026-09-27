import { describe, it, expect, vi, beforeEach } from "vitest";
import { renderHook, act } from "@testing-library/react";
import { useComposerAttachments } from "./useComposerAttachments";
import type { TenantProfile } from "../lib/types";

const profile: TenantProfile = {
  label: "t",
  tenantId: "t",
  apiKey: "k",
  baseUrl: "http://x",
};

// uploadAsset is mocked so the hook's own logic — file filtering,
// dedup, ordering, URL building, clear() — is what's under test.
vi.mock("../lib/api", () => ({
  uploadAsset: vi.fn(),
  assetUrl: vi.fn(
    (_p: TenantProfile, _pid: string, _sid: string, aid: string) =>
      `http://x/asset/${aid}`,
  ),
}));

import { uploadAsset, assetUrl } from "../lib/api";

function png(name = "x.png") {
  return new File([new Uint8Array([1, 2, 3])], name, { type: "image/png" });
}

function txt(name = "a.txt") {
  return new File([new Uint8Array([1])], name, { type: "text/plain" });
}

describe("useComposerAttachments", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    (uploadAsset as unknown as ReturnType<typeof vi.fn>).mockImplementation(
      async (_p: TenantProfile, _pid: string, _sid: string, file: File) => ({
        asset_id: `aid_${file.name}`,
        media_type: file.type,
        bytes: file.size,
      }),
    );
  });

  it("uploads an image file and adds it to pendingAssets", async () => {
    const { result } = renderHook(() =>
      useComposerAttachments(profile, "p1", "s1"),
    );
    expect(result.current.pendingAssets).toEqual([]);
    await act(async () => {
      await result.current.onFiles([png()]);
    });
    expect(result.current.pendingAssets).toHaveLength(1);
    expect(result.current.pendingAssets[0]).toEqual({
      asset_id: "aid_x.png",
      media_type: "image/png",
      url: "http://x/asset/aid_x.png",
    });
    expect(uploadAsset).toHaveBeenCalledTimes(1);
  });

  it("skips non-image files", async () => {
    const { result } = renderHook(() =>
      useComposerAttachments(profile, "p1", "s1"),
    );
    await act(async () => {
      await result.current.onFiles([txt(), png()]);
    });
    expect(result.current.pendingAssets).toHaveLength(1);
    expect(uploadAsset).toHaveBeenCalledTimes(1);
    expect(result.current.pendingAssets[0].asset_id).toBe("aid_x.png");
  });

  it("sets uploading=true while in-flight, false after", async () => {
    let resolveUpload: () => void = () => {};
    (uploadAsset as unknown as ReturnType<typeof vi.fn>).mockImplementation(
      () =>
        new Promise((res) => {
          resolveUpload = () =>
            res({
              asset_id: "blocked",
              media_type: "image/png",
              bytes: 1,
            });
        }),
    );
    const { result } = renderHook(() =>
      useComposerAttachments(profile, "p1", "s1"),
    );
    let p: Promise<void>;
    act(() => {
      p = result.current.onFiles([png()]);
    });
    // microtask flush so the hook's setUploading(true) ran
    await Promise.resolve();
    expect(result.current.uploading).toBe(true);
    await act(async () => {
      resolveUpload();
      await p!;
    });
    expect(result.current.uploading).toBe(false);
  });

  it("exposes assetIds helper for the send body", async () => {
    const { result } = renderHook(() =>
      useComposerAttachments(profile, "p1", "s1"),
    );
    await act(async () => {
      await result.current.onFiles([png("a.png"), png("b.png")]);
    });
    expect(result.current.assetIds).toEqual(["aid_a.png", "aid_b.png"]);
  });

  it("clear() removes all pending assets", async () => {
    const { result } = renderHook(() =>
      useComposerAttachments(profile, "p1", "s1"),
    );
    await act(async () => {
      await result.current.onFiles([png()]);
    });
    expect(result.current.pendingAssets).toHaveLength(1);
    act(() => {
      result.current.clear();
    });
    expect(result.current.pendingAssets).toEqual([]);
    expect(result.current.assetIds).toEqual([]);
  });

  it("remove(asset_id) drops a single pending asset", async () => {
    const { result } = renderHook(() =>
      useComposerAttachments(profile, "p1", "s1"),
    );
    await act(async () => {
      await result.current.onFiles([png("a.png"), png("b.png")]);
    });
    act(() => {
      result.current.remove("aid_a.png");
    });
    expect(result.current.assetIds).toEqual(["aid_b.png"]);
  });

  it("builds pending asset url via assetUrl helper", async () => {
    const { result } = renderHook(() =>
      useComposerAttachments(profile, "p1", "s1"),
    );
    await act(async () => {
      await result.current.onFiles([png()]);
    });
    expect(assetUrl).toHaveBeenCalledWith(
      profile,
      "p1",
      "s1",
      "aid_x.png",
    );
  });

  it("is a no-op when sid is null", async () => {
    const { result } = renderHook(() =>
      useComposerAttachments(profile, "p1", null),
    );
    await act(async () => {
      await result.current.onFiles([png()]);
    });
    expect(uploadAsset).not.toHaveBeenCalled();
    expect(result.current.pendingAssets).toEqual([]);
  });
});
