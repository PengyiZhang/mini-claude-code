import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { renderHook, act, cleanup } from "@testing-library/react";
import { useAssetUrl } from "./useAssetUrl";
import type { TenantProfile } from "./types";

const profile: TenantProfile = {
  label: "t",
  tenantId: "t",
  apiKey: "secret-key",
  baseUrl: "http://x",
};

beforeEach(() => {
  vi.stubGlobal("URL", {
    ...URL,
    createObjectURL: vi.fn(() => "blob:fake-url"),
    revokeObjectURL: vi.fn(),
  });
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

function mockFetchOk(bytes: Uint8Array | null) {
  // Build the Response from raw bytes rather than a Blob: some CI
  // environments pair jsdom's Blob (no .stream()) with undici's Response
  // (requires a spec Blob body), which explodes inside the Response
  // constructor with "object.stream is not a function". An
  // ArrayBufferView body is spec-native everywhere; the explicit
  // Content-Type header keeps resp.blob() typed.
  const body = (bytes ?? new Uint8Array(0)).buffer as ArrayBuffer;
  const fetchSpy = vi.fn().mockResolvedValue(
    new Response(body, { status: 200, headers: { "Content-Type": "image/png" } }),
  );
  vi.stubGlobal("fetch", fetchSpy);
  return fetchSpy;
}

function mockFetchStatus(status: number) {
  const fetchSpy = vi.fn().mockResolvedValue(new Response("", { status }));
  vi.stubGlobal("fetch", fetchSpy);
  return fetchSpy;
}

describe("useAssetUrl", () => {
  it("fetches with Authorization header and exposes a blob url", async () => {
    const fetchSpy = mockFetchOk(new Uint8Array([1, 2, 3]));
    const { result } = renderHook(
      ({ p, id }) => useAssetUrl(p, "proj", "sess", id),
      { initialProps: { p: profile, id: "abc123def456abcd" } },
    );
    // Initial state: no url, no error.
    expect(result.current.url).toBeNull();
    expect(result.current.error).toBeNull();

    // Wait for the fetch + blob resolution to land.
    await act(async () => {
      await new Promise((r) => setTimeout(r, 0));
    });
    expect(result.current.url).toBe("blob:fake-url");
    expect(result.current.error).toBeNull();
    expect(fetchSpy).toHaveBeenCalledTimes(1);
    const [calledUrl, init] = fetchSpy.mock.calls[0];
    expect(calledUrl).toContain("/projects/proj/sessions/sess/assets/abc123def456abcd");
    expect((init as RequestInit).headers).toEqual({
      Authorization: "Bearer secret-key",
    });
    expect(URL.createObjectURL).toHaveBeenCalledTimes(1);
  });

  it("revokes the blob url on unmount", async () => {
    mockFetchOk(new Uint8Array([1]));
    const { result, unmount } = renderHook(
      ({ p, id }) => useAssetUrl(p, "proj", "sess", id),
      { initialProps: { p: profile, id: "abc123def456abcd" } },
    );
    await act(async () => {
      await new Promise((r) => setTimeout(r, 0));
    });
    expect(result.current.url).toBe("blob:fake-url");
    expect(URL.revokeObjectURL).not.toHaveBeenCalled();

    unmount();
    expect(URL.revokeObjectURL).toHaveBeenCalledTimes(1);
    expect(URL.revokeObjectURL).toHaveBeenCalledWith("blob:fake-url");
  });

  it("surfaces load failure as error state without throwing", async () => {
    mockFetchStatus(401);
    const { result } = renderHook(
      ({ p, id }) => useAssetUrl(p, "proj", "sess", id),
      { initialProps: { p: profile, id: "abc123def456abcd" } },
    );
    await act(async () => {
      await new Promise((r) => setTimeout(r, 0));
    });
    expect(result.current.url).toBeNull();
    expect(result.current.error).toBeInstanceOf(Error);
    expect(result.current.error?.message).toContain("401");
    expect(URL.createObjectURL).not.toHaveBeenCalled();
  });

  it("no-ops when assetId is null", async () => {
    const fetchSpy = mockFetchOk(null);
    const { result } = renderHook(
      ({ p, id }) => useAssetUrl(p, "proj", "sess", id),
      { initialProps: { p: profile, id: null } },
    );
    await act(async () => {
      await new Promise((r) => setTimeout(r, 0));
    });
    expect(result.current.url).toBeNull();
    expect(result.current.error).toBeNull();
    expect(fetchSpy).not.toHaveBeenCalled();
  });

  it("revokes the previous blob when assetId changes", async () => {
    mockFetchOk(new Uint8Array([1]));
    const { result, rerender } = renderHook(
      ({ p, id }) => useAssetUrl(p, "proj", "sess", id),
      { initialProps: { p: profile, id: "abc123def456abcd" } },
    );
    await act(async () => {
      await new Promise((r) => setTimeout(r, 0));
    });
    expect(result.current.url).toBe("blob:fake-url");

    // Switch assetId → cleanup of the previous effect must revoke.
    rerender({ p: profile, id: "deadbeefdeadbeef" });
    await act(async () => {
      await new Promise((r) => setTimeout(r, 0));
    });
    expect(URL.revokeObjectURL).toHaveBeenCalledWith("blob:fake-url");
  });
});
