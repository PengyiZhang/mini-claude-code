import { useState } from "react";
import type { CardListItem, CardListPayload, CardEvent } from "../../lib/types";
import { useChat } from "../../lib/store";
import { CardIcon } from "./icons";
import { CardView } from "./index";

interface CardListProps {
  payload: CardListPayload;
  parentCardId?: string;
  chatKey?: string;
}

export function CardList({ payload, parentCardId, chatKey }: CardListProps) {
  const items = payload.items ?? [];
  const [expandedRow, setExpandedRow] = useState<string | null>(null);

  if (items.length === 0) {
    return (
      <div className="text-xs text-ink-dim italic">
        {payload.empty_hint ?? "no items"}
      </div>
    );
  }
  return (
    <div className="space-y-2">
      <ul className="space-y-1">
        {items.map((it) => (
          <ListItem
            key={it.id}
            item={it}
            expandedRow={expandedRow}
            onToggleExpand={(rowId, cmd) => {
              const expanding = expandedRow !== rowId;
              setExpandedRow(expanding ? rowId : null);
              if (expanding && cmd) {
                useChat.getState().runCommand(cmd);
              }
            }}
            parentCardId={parentCardId}
            chatKey={chatKey}
          />
        ))}
      </ul>
      {payload.summary && (
        <div className="text-xs text-ink-dim italic border-t border-border pt-1">
          {payload.summary}
        </div>
      )}
    </div>
  );
}

interface ListItemProps {
  item: CardListItem;
  expandedRow: string | null;
  onToggleExpand: (rowId: string, cmd: string | null | undefined) => void;
  parentCardId?: string;
  chatKey?: string;
}

function ListItem({ item, expandedRow, onToggleExpand, parentCardId, chatKey }: ListItemProps) {
  const runCommand = useChat((s) => s.runCommand);
  const clickable = Boolean(item.expandable_command);
  const isExpanded = expandedRow === item.id;

  const titleNode = (
    <>
      <CardIcon name={item.icon} />
      <span className="font-medium">{item.title}</span>
      {item.subtitle && (
        <span className="text-xs text-ink-dim">
          — <span>{item.subtitle}</span>
        </span>
      )}
      {item.badges.map((b, i) => (
        <span
          key={`${b.text}-${i}`}
          className={`text-[10px] uppercase tracking-wide px-1.5 py-0.5 rounded border ${badgeTone(b.tone)}`}
        >
          {b.text}
        </span>
      ))}
      {item.meta && (
        <span className="ml-auto text-xs text-ink-dim">{item.meta}</span>
      )}
      {clickable && (
        <span className={`ml-2 text-xs text-ink-dim transition-transform ${isExpanded ? "rotate-90" : ""}`}>▶</span>
      )}
    </>
  );

  return (
    <li className="border border-border rounded bg-bg-card overflow-hidden">
      {clickable ? (
        <button
          type="button"
          onClick={() => onToggleExpand(item.id, item.expandable_command)}
          className="w-full flex items-center gap-2 px-3 py-2 text-left hover:bg-bg-hover"
        >
          {titleNode}
        </button>
      ) : (
        <div className="w-full flex items-center gap-2 px-3 py-2">{titleNode}</div>
      )}
      {isExpanded && parentCardId && chatKey && (
        <ChildCardSlot parentCardId={parentCardId} rowId={item.id} chatKey={chatKey} />
      )}
      {item.menu.length > 0 && (
        <div className="flex items-center gap-1 px-3 py-1.5 border-t border-border bg-bg-hover/40">
          {item.menu.map((a, i) => (
            <button
              key={`${a.label}-${i}`}
              type="button"
              onClick={() => runCommand(a.command)}
              className={`text-xs px-2 py-0.5 rounded border ${actionTone(a.tone)}`}
            >
              {a.label}
            </button>
          ))}
        </div>
      )}
    </li>
  );
}

function ChildCardSlot({
  parentCardId,
  rowId,
  chatKey,
}: {
  parentCardId: string;
  rowId: string;
  chatKey: string;
}) {
  const childId = `${parentCardId}::${rowId}`;
  const child = useChat((s) => {
    const msgs = s.messages[chatKey] ?? [];
    for (let i = msgs.length - 1; i >= 0; i--) {
      const cards = msgs[i].cards;
      const found = cards?.find((c) => c.id === childId);
      if (found) return found;
    }
    return null;
  }) as CardEvent | null;

  if (!child) {
    return (
      <div className="px-3 py-2 border-t border-border text-xs text-ink-dim italic">
        Loading…
      </div>
    );
  }
  return (
    <div className="px-3 py-2 border-t border-border bg-bg-hover/20">
      <CardView card={child} />
    </div>
  );
}

function badgeTone(tone: CardListItem["badges"][number]["tone"]): string {
  switch (tone) {
    case "ok": return "text-ok border-ok/40 bg-ok/10";
    case "warn": return "text-warn border-warn/40 bg-warn/10";
    case "err": return "text-err border-err/40 bg-err/10";
    case "accent": return "text-accent border-accent/40 bg-accent/10";
    default: return "text-ink-dim border-border";
  }
}

function actionTone(tone: CardListItem["menu"][number]["tone"]): string {
  switch (tone) {
    case "ok": return "border-ok/40 text-ok hover:bg-ok/10";
    case "warn": return "border-warn/40 text-warn hover:bg-warn/10";
    case "err": return "border-err/40 text-err hover:bg-err/10";
    case "accent": return "border-accent/40 text-accent hover:bg-accent/10";
    default: return "border-border text-ink-dim hover:bg-bg-hover";
  }
}
