import type { TenantProfile } from "../lib/types";
import type { PendingAssetRef } from "../lib/store";
import UserImage from "./UserImage";

interface Props {
  text: string;
  assets?: PendingAssetRef[];
  profile: TenantProfile;
  pid: string;
  sid: string;
}

/**
 * Render the inside of a user chat bubble.
 *
 * Two modes (Task 14):
 *   1. Pure text (the historical default): `{text}` rendered verbatim.
 *   2. Text + attached images: text on top, then one `<UserImage>` per
 *      asset.
 *
 * Image rendering (Bug B fix): browsers cannot attach `Authorization:
 * Bearer` to `<img>` requests, so we delegate to `<UserImage>` which
 * fetches the bytes via the authenticated `useAssetUrl` hook and feeds
 * the resulting `blob:` URL to `<img src>`.
 *
 * Extracted as a pure-ish component so it can be tested without
 * dragging in the full Workspace store/zustand graph; the only state
 * lives inside `<UserImage>` via the hook.
 */
export default function UserMessageContent({
  text,
  assets,
  profile,
  pid,
  sid,
}: Props) {
  return (
    <div className="space-y-1">
      {text ? <div className="whitespace-pre-wrap break-words">{text}</div> : null}
      {(assets ?? []).map((a) => (
        <UserImage
          key={a.asset_id}
          profile={profile}
          pid={pid}
          sid={sid}
          assetId={a.asset_id}
        />
      ))}
    </div>
  );
}
