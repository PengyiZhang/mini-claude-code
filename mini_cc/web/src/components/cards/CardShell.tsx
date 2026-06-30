import { useState } from "react";
import type { CardAction, CardStatus } from "../../lib/types";
import { CardIcon } from "./icons";
import { useChat } from "../../lib/store";
import { AgentsSpawnForm } from "./AgentsSpawnForm";

interface CardShellProps {
  title?: string | null;
  icon?: string | null;
  status: CardStatus;
  error_message?: string | null;
  // Optional footer action buttons. Rendered here so all variants share
  // the same action styling. Wired to runCommand in Task 1.5.
  actions?: CardAction[];
  children?: React.ReactNode;
}

const STATUS_RING: Record<CardStatus, string> = {
  ok: "border-border",
  warning: "border-warn/50",
  error: "border-err/50",
};

const STATUS_BADGE: Record<CardStatus, { label: string; cls: string }> = {
  ok: { label: "", cls: "" },
  warning: { label: "warning", cls: "text-warn bg-warn/10 border border-warn/30" },
  error: { label: "error", cls: "text-err bg-err/10 border border-err/30" },
};

// Heuristic: an action whose command matches this pattern opens the
// inline spawn form instead of firing the raw slash. The raw command
// would error (spawn needs 3 args), so we intercept it and surface a
// proper form. Future card-level forms should grow a typed `form` field
// on CardAction rather than piling on more patterns here.
const SPAWN_ACTION_RE = /^\/agents\s+spawn(\s|$)/;

export function CardShell({
  title,
  icon,
  status,
  error_message,
  actions,
  children,
}: CardShellProps) {
  const [collapsed, setCollapsed] = useState(false);
  const [showSpawnForm, setShowSpawnForm] = useState(false);
  const badge = STATUS_BADGE[status];
  const runCommand = useChat((s) => s.runCommand);

  return (
    <section
      className={`rounded-md border ${STATUS_RING[status]} bg-bg-card text-ink overflow-hidden`}
      aria-label={title ?? "card"}
    >
      <header
        className="flex items-center gap-2 px-3 py-2 bg-bg-hover/60 cursor-pointer select-none"
        onClick={() => setCollapsed((v) => !v)}
      >
        <CardIcon name={icon} />
        {title && (
          <span className="font-semibold text-sm truncate">{title}</span>
        )}
        {badge.label && (
          <span
            className={`text-[10px] uppercase tracking-wide px-1.5 py-0.5 rounded ${badge.cls}`}
          >
            {badge.label}
          </span>
        )}
        <span className="ml-auto text-xs text-ink-dim">
          {collapsed ? "▶" : "▼"}
        </span>
      </header>

      {!collapsed && (
        <>
          {status === "error" && error_message && (
            <div className="px-3 py-2 text-xs text-err bg-err/10 border-b border-err/30">
              {error_message}
            </div>
          )}
          <div className="px-3 py-2 text-sm">{children}</div>
          {actions && actions.length > 0 && (
            <footer className="flex items-center gap-2 px-3 py-2 border-t border-border bg-bg-hover/40">
              {actions.map((a, i) => (
                <button
                  key={`${a.label}-${i}`}
                  type="button"
                  onClick={(e) => {
                    e.stopPropagation();
                    if (SPAWN_ACTION_RE.test(a.command)) {
                      setShowSpawnForm((v) => !v);
                    } else {
                      runCommand(a.command);
                    }
                  }}
                  className={`text-xs px-2 py-1 rounded border ${toneClass(a.tone)}`}
                >
                  {a.label}
                </button>
              ))}
            </footer>
          )}
          {showSpawnForm && (
            <AgentsSpawnForm onClose={() => setShowSpawnForm(false)} />
          )}
        </>
      )}
    </section>
  );
}

function toneClass(tone: CardAction["tone"]): string {
  switch (tone) {
    case "ok":
      return "border-ok/40 text-ok hover:bg-ok/10";
    case "warn":
      return "border-warn/40 text-warn hover:bg-warn/10";
    case "err":
      return "border-err/40 text-err hover:bg-err/10";
    case "accent":
    case "default":
    default:
      return "border-accent/40 text-accent hover:bg-accent/10";
  }
}
