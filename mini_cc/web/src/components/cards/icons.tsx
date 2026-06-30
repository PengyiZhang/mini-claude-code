// CardIcon: maps the closed ICON_KEYS enum (mirrors
// mini_cc/commands/cards.py) to a glyph. We keep this as an emoji map
// for now — cheap to ship, good enough visually. Phase B can swap to
// inline SVGs without touching call sites.
import type { JSX } from "react";

const GLYPHS: Record<string, string> = {
  agents: "👥",
  bg: "🌀",
  loop: "🔁",
  workflow: "🧩",
  config: "⚙️",
  help: "❓",
  tools: "🔧",
  mcp: "🔌",
  sessions: "🗂️",
  logs: "📜",
  tasks: "✅",
  skills: "🎯",
  search: "🔍",
  permissions: "🛡️",
  cost: "💰",
  model: "🤖",
};

export function CardIcon({ name }: { name: string | null | undefined }) {
  if (!name) return null;
  const glyph = GLYPHS[name];
  return (
    <span
      className="text-base leading-none"
      aria-hidden={glyph ? undefined : true}
      title={glyph ? undefined : `unknown icon: ${name}`}
    >
      {glyph ?? "▪"}
    </span>
  );
}

export function CardIconKeyList(): readonly string[] {
  return Object.keys(GLYPHS);
}

// Helper so JSX namespace import is actually referenced even when
// future refactors drop the JSX.Element return annotation.
export type _IconMarker = JSX.Element;
