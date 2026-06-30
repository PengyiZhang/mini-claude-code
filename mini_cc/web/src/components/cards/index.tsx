import type { CardEvent } from "../../lib/types";
import { CardShell } from "./CardShell";
import { CardList } from "./CardList";
import { CardKeyValue } from "./CardKeyValue";
import { useLiveRefresh } from "./useLiveRefresh";

interface CardViewProps {
  card: CardEvent;
  parentCardId?: string;
  chatKey?: string;
}

// CardView: top-level dispatcher. Each variant lands in a dedicated
// component (CardList, CardKeyValue, …) wrapped by CardShell for the
// chrome (title, icon, status, actions). CardTable and CardSteps arrive
// in Plan B. parentCardId/chatKey thread through to CardList so an
// expandable row can find its inline child card (keyed
// `${parentCardId}::${rowId}`) in the store.
export function CardView({ card, parentCardId, chatKey }: CardViewProps) {
  // Polls /bg, /agents etc. on a timer while this card is mounted and
  // refresh_command is set. No-op on static cards.
  useLiveRefresh(card, chatKey);
  return (
    <CardShell
      title={card.title}
      icon={card.icon}
      status={card.status}
      error_message={card.error_message}
      actions={card.actions}
    >
      {card.variant === "list" && (
        <CardList
          payload={card.payload as never}
          parentCardId={parentCardId}
          chatKey={chatKey}
        />
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
