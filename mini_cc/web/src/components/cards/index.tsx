import type { CardEvent } from "../../lib/types";
import { CardShell } from "./CardShell";
import { CardList } from "./CardList";
import { CardKeyValue } from "./CardKeyValue";

// CardView: top-level dispatcher. Each variant lands in a dedicated
// component (CardList, CardKeyValue, …) wrapped by CardShell for the
// chrome (title, icon, status, actions). CardTable and CardSteps arrive
// in Plan B.
export function CardView({ card }: { card: CardEvent }) {
  return (
    <CardShell
      title={card.title}
      icon={card.icon}
      status={card.status}
      error_message={card.error_message}
      actions={card.actions}
    >
      {card.variant === "list" && (
        <CardList payload={card.payload as never} />
      )}
      {card.variant === "key_value" && (
        <CardKeyValue payload={card.payload as never} />
      )}
      {card.variant === "table" && (
        <div className="text-xs text-ink-dim italic">(table — Plan B)</div>
      )}
      {card.variant === "steps" && (
        <div className="text-xs text-ink-dim italic">(steps — Plan B)</div>
      )}
    </CardShell>
  );
}
