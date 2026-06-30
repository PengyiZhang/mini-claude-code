// Minimal stub — full renderer lands in Phase 3.2.
import type { CardListPayload } from "../../lib/types";

export function CardList({ payload }: { payload: CardListPayload }) {
  const items = payload.items ?? [];
  if (items.length === 0) {
    return (
      <div className="text-xs text-ink-dim italic">
        {payload.empty_hint ?? "no items"}
      </div>
    );
  }
  return (
    <ul className="space-y-1">
      {items.map((it) => (
        <li key={it.id} className="text-sm">
          {it.title}
        </li>
      ))}
    </ul>
  );
}
