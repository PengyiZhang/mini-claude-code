import { useState } from "react";
import type { CardKeyValuePair, CardKeyValuePayload } from "../../lib/types";

export function CardKeyValue({ payload }: { payload: CardKeyValuePayload }) {
  const pairs = payload.pairs ?? [];
  if (pairs.length === 0) {
    return <div className="text-xs text-ink-dim italic">no entries</div>;
  }
  return (
    <dl className="grid grid-cols-[max-content_1fr] gap-x-3 gap-y-1">
      {pairs.map((p, i) => (
        <PairRow key={`${p.k}-${i}`} pair={p} />
      ))}
    </dl>
  );
}

function PairRow({ pair }: { pair: CardKeyValuePair }) {
  const [revealed, setRevealed] = useState(false);
  const sensitive = pair.sensitive && !revealed;
  // Mask is a fixed-width opaque blob so the value's rough length
  // isn't leaked (some secrets are conspicuously short).
  const MASK = "••••••••";
  const value = sensitive ? MASK : pair.v;
  return (
    <div className="contents">
      <dt className="text-xs text-ink-dim">{pair.k}</dt>
      <dd className={`text-sm ${pair.mono ? "font-mono" : ""} flex items-center gap-2`}>
        {pair.mono ? <code>{value}</code> : <span>{value}</span>}
        {pair.sensitive && (
          <button
            type="button"
            onClick={() => setRevealed((v) => !v)}
            className="text-xs text-ink-dim hover:text-ink border border-border rounded px-1 py-0.5"
            aria-label={revealed ? "hide" : "show"}
            title={revealed ? "hide value" : "reveal value"}
          >
            {revealed ? "◉" : "◯"}
          </button>
        )}
      </dd>
    </div>
  );
}
