import { useEffect, useRef } from "react";

export interface MentionCandidate {
  name: string;
  role: string;
}

interface Props {
  /** Text typed after the `@`. Empty string = bare `@` or nothing-after. */
  query: string;
  candidates: MentionCandidate[];
  activeIndex: number;
  onPick: (name: string) => void;
  onClose: () => void;
  /** Force-show all candidates regardless of query (test affordance). */
  initialOpen?: boolean;
}

/**
 * MentionPicker — @ autocomplete for alive teammates.
 *
 * Pops up in the ChatPane textarea when the user types `@<partial>`.
 * Visibility rules:
 *   - empty query → render nothing (caller decides to show on bare `@`)
 *   - no prefix match → render nothing
 *   - otherwise list prefix-matched candidates
 *
 * The caller owns keyboard navigation (ArrowUp/Down/Enter/Escape) and
 * passes activeIndex; this component is purely presentational + click
 * routing. Clicking a row calls onPick(name).
 */
export default function MentionPicker({
  query,
  candidates,
  activeIndex,
  onPick,
  onClose,
  initialOpen,
}: Props) {
  const containerRef = useRef<HTMLDivElement | null>(null);

  // Close on outside click. Caller is responsible for unmounting.
  useEffect(() => {
    if (!initialOpen) return;
    const onDoc = (e: MouseEvent) => {
      if (!containerRef.current?.contains(e.target as Node)) {
        onClose();
      }
    };
    document.addEventListener("mousedown", onDoc);
    return () => document.removeEventListener("mousedown", onDoc);
  }, [initialOpen, onClose]);

  const q = query.trim().toLowerCase();
  const filtered = initialOpen
    ? candidates
    : q
      ? candidates.filter((c) => c.name.toLowerCase().startsWith(q))
      : [];
  if (filtered.length === 0) return null;

  return (
    <div
      ref={containerRef}
      className="absolute bottom-full mb-2 left-0 right-0 max-w-md mx-auto bg-bg-panel border border-border rounded-md shadow-lg overflow-hidden z-30"
      role="listbox"
      aria-label="Mention teammates"
    >
      <div className="px-2 py-1 text-[10px] uppercase tracking-wide text-ink-faint border-b border-border">
        Mention teammate
      </div>
      <div className="max-h-64 overflow-auto">
        {filtered.map((c, i) => (
          <button
            key={c.name}
            type="button"
            onClick={() => onPick(c.name)}
            onMouseEnter={(e) => e.currentTarget.focus()}
            className={
              "w-full flex items-center gap-2 px-2 py-1.5 text-left text-sm " +
              (i === activeIndex
                ? "bg-accent text-white"
                : "hover:bg-bg-hover text-ink")
            }
            role="option"
            aria-selected={i === activeIndex}
          >
            <span
              className={
                "size-6 rounded text-xs flex items-center justify-center font-semibold uppercase shrink-0 " +
                (i === activeIndex
                  ? "bg-white/20 text-white"
                  : "bg-gradient-to-br from-emerald-500 to-teal-400 text-white")
              }
            >
              {c.name.slice(0, 1)}
            </span>
            <div className="flex-1 min-w-0">
              <div className="truncate">@{c.name}</div>
              {c.role && (
                <div
                  className={
                    "text-xs truncate " +
                    (i === activeIndex ? "text-white/80" : "text-ink-dim")
                  }
                >
                  {c.role}
                </div>
              )}
            </div>
          </button>
        ))}
      </div>
    </div>
  );
}
