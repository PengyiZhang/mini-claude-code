import { useCallback, useMemo, useState } from "react";
import { assetUrl, uploadAsset } from "../lib/api";
import type { AssetRef, TenantProfile } from "../lib/types";

// Pending image attachment held in the chat composer before send. The
// `url` field is the canonical GET URL (tenant-scoped) so the thumbnail
// chip can render without re-deriving it. `asset_id` is the opaque token
// the backend hands back from POST /sessions/{sid}/assets — it gets
// threaded through the next /send body under `assets: [...]` when the
// user submits.
export interface PendingAsset {
  asset_id: string;
  media_type: string;
  url: string;
}

// Extracted from Workspace.tsx so the upload/filter/dedup logic is
// unit-testable without rendering the entire component. Workspace
// consumes the hook's surface and wires its values into the send()
// handler and the composer JSX (file picker, drag/drop, paste, chip
// list). pid/profile/sid match Workspace's own props/state 1:1.
export function useComposerAttachments(
  profile: TenantProfile,
  pid: string,
  sid: string | null,
) {
  const [pendingAssets, setPendingAssets] = useState<PendingAsset[]>([]);
  const [uploading, setUploading] = useState(false);

  const onFiles = useCallback(
    async (files: FileList | File[]) => {
      if (!sid) return;
      // Only image MIME types are accepted — the backend AssetStore
      // rejects anything else, so we filter client-side to avoid a
      // wasted round-trip and a confusing error toast.
      const images = Array.from(files).filter((f) =>
        f.type.startsWith("image/"),
      );
      if (images.length === 0) return;
      setUploading(true);
      try {
        for (const file of images) {
          const ref: AssetRef = await uploadAsset(profile, pid, sid, file);
          setPendingAssets((prev) => [
            ...prev,
            {
              asset_id: ref.asset_id,
              media_type: ref.media_type,
              url: assetUrl(profile, pid, sid, ref.asset_id),
            },
          ]);
        }
      } finally {
        setUploading(false);
      }
    },
    [profile, pid, sid],
  );

  const clear = useCallback(() => setPendingAssets([]), []);

  const remove = useCallback((assetId: string) => {
    setPendingAssets((prev) => prev.filter((a) => a.asset_id !== assetId));
  }, []);

  const assetIds = useMemo(
    () => pendingAssets.map((a) => a.asset_id),
    [pendingAssets],
  );

  return { pendingAssets, uploading, onFiles, clear, remove, assetIds };
}
