import { useEffect, useState } from "react";
import { assetUrl, authHeaders } from "./api";
import type { TenantProfile } from "./types";

/**
 * Fetch an authenticated asset binary and expose it as a `blob:` URL.
 *
 * `<img src={assetUrl(...)}>` fails because browsers cannot attach the
 * `Authorization: Bearer` header to image-element requests — the server
 * sees no bearer and returns 401. This hook fetches the bytes via
 * `fetch()` (which CAN carry the header), wraps them in a blob URL, and
 * hands that to the caller for use as the `<img src>`. The blob is
 * revoked on unmount or when any dep changes — leaking blob URLs holds
 * the binary in memory until the document unloads.
 *
 * Returns `{url: null, error: null}` while the fetch is in flight,
 * `{url: "blob:...", error: null}` on success, and
 * `{url: null, error: Error}` on failure (non-2xx, network drop, etc).
 * Pass `assetId: null` to no-op (renders nothing).
 */
export function useAssetUrl(
  profile: TenantProfile,
  pid: string,
  sid: string,
  assetId: string | null,
): { url: string | null; error: Error | null } {
  const [url, setUrl] = useState<string | null>(null);
  const [error, setError] = useState<Error | null>(null);

  useEffect(() => {
    if (!assetId) {
      setUrl(null);
      setError(null);
      return;
    }
    let cancelled = false;
    let blobUrl: string | null = null;
    setUrl(null);
    setError(null);
    fetch(assetUrl(profile, pid, sid, assetId),
          { headers: authHeaders(profile) })
      .then(async (r) => {
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        return URL.createObjectURL(await r.blob());
      })
      .then((u) => {
        if (cancelled) {
          // Component unmounted mid-flight; don't leak the blob.
          URL.revokeObjectURL(u);
          return;
        }
        blobUrl = u;
        setUrl(u);
      })
      .catch((e: unknown) => {
        if (!cancelled) setError(e instanceof Error ? e : new Error(String(e)));
      });
    return () => {
      cancelled = true;
      if (blobUrl) URL.revokeObjectURL(blobUrl);
    };
    // profile / pid / sid identity is stable per TenantProfile object;
    // re-run only when assetId changes. Including profile/pid/sid in
    // deps would re-fetch every render if the parent builds a fresh
    // profile object, which some test setups do.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [assetId, profile.apiKey, pid, sid]);

  return { url, error };
}
