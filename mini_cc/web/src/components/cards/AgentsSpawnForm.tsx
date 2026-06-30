import { useState } from "react";
import { useChat } from "../../lib/store";

/**
 * Inline form for `/agents spawn <name> <role> --prompt <text>`.
 *
 * Triggered by the "+ spawn" action on the /agents-roster card.
 * Replaces the click-through-to-slash-autocompletion hack from
 * Phase 3 with a real two-field form so the user doesn't have to
 * remember the positional argument order.
 *
 * On submit, builds the slash command and dispatches through
 * runCommand — same path as a manually-typed slash. Errors bubble
 * up through the normal command stream (no local validation beyond
 * "fields aren't empty").
 *
 * Closes on successful submit (form toggles back to the action row).
 */
export function AgentsSpawnForm({ onClose }: { onClose: () => void }) {
  const runCommand = useChat((s) => s.runCommand);
  const [name, setName] = useState("");
  const [role, setRole] = useState("");
  const [prompt, setPrompt] = useState("");
  const [error, setError] = useState<string | null>(null);

  const submit = () => {
    const n = name.trim();
    const r = role.trim();
    const p = prompt.trim();
    if (!n || !r || !p) {
      setError("name, role, and prompt are all required");
      return;
    }
    // Quote the prompt so spaces survive the slash-command parser
    // (registry._parse_spawn_args honours surrounding quotes).
    runCommand(`/agents spawn ${n} ${r} --prompt "${p.replace(/"/g, "\\\"")}"`);
    onClose();
  };

  return (
    <div
      className="px-3 py-3 border-t border-border bg-bg-hover/40 space-y-2"
      onClick={(e) => e.stopPropagation()}
    >
      <div className="text-xs text-ink-dim">Spawn a teammate</div>
      <div className="grid grid-cols-2 gap-2">
        <label className="text-xs flex flex-col gap-1">
          <span className="text-ink-dim">name</span>
          <input
            className="bg-bg border border-border rounded px-2 py-1 text-sm"
            value={name}
            onChange={(e) => { setName(e.target.value); setError(null); }}
            placeholder="alice"
            autoFocus
          />
        </label>
        <label className="text-xs flex flex-col gap-1">
          <span className="text-ink-dim">role</span>
          <input
            className="bg-bg border border-border rounded px-2 py-1 text-sm"
            value={role}
            onChange={(e) => { setRole(e.target.value); setError(null); }}
            placeholder="researcher"
          />
        </label>
      </div>
      <label className="text-xs flex flex-col gap-1">
        <span className="text-ink-dim">prompt</span>
        <textarea
          className="bg-bg border border-border rounded px-2 py-1 text-sm font-mono"
          rows={3}
          value={prompt}
          onChange={(e) => { setPrompt(e.target.value); setError(null); }}
          placeholder="Find the latest test results and summarize them."
        />
      </label>
      {error && (
        <div className="text-xs text-err">{error}</div>
      )}
      <div className="flex items-center justify-end gap-2 pt-1">
        <button
          type="button"
          onClick={onClose}
          className="text-xs px-2 py-1 rounded border border-border text-ink-dim hover:bg-bg-hover"
        >
          cancel
        </button>
        <button
          type="button"
          onClick={submit}
          aria-label="spawn teammate"
          className="text-xs px-2 py-1 rounded border border-accent/40 text-accent hover:bg-accent/10"
        >
          spawn teammate
        </button>
      </div>
    </div>
  );
}
