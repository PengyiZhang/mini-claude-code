// Minimal stub — full renderer with sensitive masking lands in Phase 2.2.
import type { CardKeyValuePayload } from "../../lib/types";

export function CardKeyValue({ payload }: { payload: CardKeyValuePayload }) {
  const pairs = payload.pairs ?? [];
  if (pairs.length === 0) {
    return <div className="text-xs text-ink-dim italic">no entries</div>;
  }
  return (
    <dl className="grid grid-cols-[max-content_1fr] gap-x-3 gap-y-1">
      {pairs.map((p, i) => (
        <div key={i} className="contents">
          <dt className="text-xs text-ink-dim">{p.k}</dt>
          <dd className={`text-sm ${p.mono ? "font-mono" : ""}`}>{p.v}</dd>
        </div>
      ))}
    </dl>
  );
}
