import { useEffect, useRef } from "react";
import type { CommandDef } from "../lib/commands";

interface Props {
  commands: CommandDef[];            // already filtered + sliced to top N
  active: number;                    // highlighted index
  onHover: (idx: number) => void;
  onPick: (cmd: CommandDef) => void;
  emptyHint?: string;
}

export default function SlashMenu({
  commands,
  active,
  onHover,
  onPick,
  emptyHint = "no matching command — press Esc to dismiss",
}: Props) {
  const listRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!listRef.current) return;
    const el = listRef.current.querySelector<HTMLElement>(`[data-idx="${active}"]`);
    el?.scrollIntoView({ block: "nearest" });
  }, [active]);

  if (commands.length === 0) {
    return (
      <div className="absolute bottom-full mb-2 left-0 w-80 bg-bg-panel border border-border rounded shadow-lg z-20 p-2 text-xs text-ink-dim">
        {emptyHint}
      </div>
    );
  }

  return (
    <div
      ref={listRef}
      className="absolute bottom-full mb-2 left-0 w-80 max-h-72 overflow-auto bg-bg-panel border border-border rounded shadow-lg z-20"
    >
      <div className="text-xs text-ink-faint uppercase tracking-wide px-3 pt-2 pb-1 border-b border-border sticky top-0 bg-bg-panel">
        commands
      </div>
      {commands.map((c, i) => (
        <button
          key={c.name}
          data-idx={i}
          onMouseEnter={() => onHover(i)}
          onMouseDown={(e) => {
            e.preventDefault();
            onPick(c);
          }}
          className={`w-full text-left px-3 py-2 flex flex-col gap-0.5 ${
            i === active ? "bg-accent/20" : "hover:bg-bg-hover"
          }`}
        >
          <div className="flex items-center gap-2 text-sm">
            <span className="font-mono text-accent">/{c.name}</span>
            {c.aliases.length > 0 && (
              <span className="text-xs text-ink-faint font-mono">
                {c.aliases.map((a) => `/${a}`).join(" ")}
              </span>
            )}
          </div>
          <div className="text-xs text-ink-dim truncate">{c.description}</div>
        </button>
      ))}
      <div className="text-xs text-ink-faint px-3 py-1.5 border-t border-border bg-bg-panel/80">
        ↑↓ navigate · ↵ run · Tab autocomplete · Esc dismiss
      </div>
    </div>
  );
}
