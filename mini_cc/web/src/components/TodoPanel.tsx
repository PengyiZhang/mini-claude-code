import { useTodos } from "../lib/store";
import type { TodoItem } from "../lib/types";

interface Props {
  chatKey: string;
}

// Stable empty-array reference. Returning `s.byKey[k] ?? []` inline
// would create a fresh array every render and trip Zustand's
// Object.is equality check into an infinite update loop.
const EMPTY: TodoItem[] = [];

const STATUS_GLYPH: Record<TodoItem["status"], string> = {
  pending: "○",
  in_progress: "◐",
  completed: "✓",
};

const STATUS_COLOR: Record<TodoItem["status"], string> = {
  pending: "text-ink-faint",
  in_progress: "text-accent",
  completed: "text-emerald-500",
};

export default function TodoPanel({ chatKey }: Props) {
  const todos = useTodos((s) => s.byKey[chatKey] ?? EMPTY);
  const collapsed = useTodos((s) => s.collapsed[chatKey] ?? false);
  const toggle = useTodos((s) => s.toggleCollapsed);

  // Empty list → hide entirely. Saves vertical space and avoids a
  // "no tasks" banner competing with the chat input.
  if (todos.length === 0) return null;

  const done = todos.filter((t) => t.status === "completed").length;
  const inProgress = todos.filter((t) => t.status === "in_progress").length;

  return (
    <div className="border-b border-border bg-bg-panel/60 text-sm">
      <div className="flex items-center justify-between px-3 py-1.5">
        <button
          onClick={() => toggle(chatKey)}
          className="flex items-center gap-2 hover:text-ink"
          title={collapsed ? "Expand task list" : "Collapse task list"}
        >
          <span className="text-xs">📋</span>
          <span className="font-medium">Tasks</span>
          <span className="text-xs text-ink-faint font-mono">
            {done}/{todos.length}
            {inProgress > 0 && ` · ${inProgress} active`}
          </span>
        </button>
        <button
          onClick={() => toggle(chatKey)}
          className="text-xs text-ink-faint hover:text-ink px-1"
          title={collapsed ? "Expand" : "Collapse"}
        >
          {collapsed ? "▴" : "▾"}
        </button>
      </div>
      {!collapsed && (
        <ul className="max-h-48 overflow-auto px-3 pb-2 flex flex-col gap-0.5">
          {todos.map((t, i) => (
            <li
              key={`${i}-${t.content.slice(0, 20)}`}
              className="flex items-start gap-2 leading-snug"
            >
              <span className={`font-mono ${STATUS_COLOR[t.status]}`}>
                {STATUS_GLYPH[t.status]}
              </span>
              <span
                className={`flex-1 ${
                  t.status === "completed"
                    ? "line-through text-ink-faint"
                    : t.status === "in_progress"
                      ? "text-ink"
                      : "text-ink-dim"
                }`}
              >
                {t.content}
              </span>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
