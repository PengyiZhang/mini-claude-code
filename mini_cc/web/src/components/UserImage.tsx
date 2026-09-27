import type { TenantProfile } from "../lib/types";
import { useAssetUrl } from "../lib/useAssetUrl";

interface Props {
  profile: TenantProfile;
  pid: string;
  sid: string;
  assetId: string;
  /** className applied to the <img> once the blob URL resolves. Also
   * drives the placeholder skeleton's dimensions when present
   * (callers that want a fixed-size chip should pass e.g.
   * `"w-12 h-12 object-cover"). */
  className?: string;
}

/**
 * Authenticated single-image render.
 *
 * Browsers cannot attach `Authorization: Bearer` to `<img>` requests,
 * so we fetch the bytes via the `useAssetUrl` hook and feed the
 * resulting `blob:` URL to `<img src>`. The hook owns the blob
 * lifecycle (revokes on unmount or asset-id change).
 *
 * Shared between:
 *   - UserMessageContent (chat bubble for the user's image attachment)
 *   - Workspace composer (thumbnail chip preview before send)
 *
 * Extracted to its own file so callers don't need to duplicate the
 * error / loading fallback markup.
 */
export default function UserImage({
  profile,
  pid,
  sid,
  assetId,
  className,
}: Props) {
  const { url, error } = useAssetUrl(profile, pid, sid, assetId);
  if (error) {
    return (
      <div className="text-destructive text-xs">[image: failed to load]</div>
    );
  }
  if (!url) {
    return (
      <div
        className={`animate-pulse bg-muted rounded ${className ?? "h-32 w-32"}`}
        aria-label="loading image"
      />
    );
  }
  return <img src={url} alt="" className={className ?? "max-w-64 rounded border border-border/50"} />;
}
