import { useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { useAuth } from "../lib/store";

function mask(key: string) {
  if (key.length <= 8) return key;
  return `${key.slice(0, 6)}…${key.slice(-4)}`;
}

export default function Tenants() {
  const profiles = useAuth((s) => s.profiles);
  const active = useAuth((s) => s.activeApiKey);
  const setActive = useAuth((s) => s.setActive);
  const remove = useAuth((s) => s.remove);
  const nav = useNavigate();
  const [revealed, setRevealed] = useState<Record<string, boolean>>({});

  return (
    <div className="min-h-screen">
      <header className="flex items-center justify-between border-b border-border bg-bg-panel px-6 py-3">
        <Link to="/projects" className="flex items-center gap-2">
          <div className="size-7 rounded-md bg-gradient-to-br from-accent to-indigo-400" />
          <span className="font-semibold">mini_cc</span>
          <span className="text-ink-dim text-sm">/ tenants</span>
        </Link>
        <Link
          to="/"
          className="text-sm px-3 py-1 rounded border border-border hover:border-accent"
        >
          add tenant
        </Link>
      </header>
      <main className="max-w-4xl mx-auto px-6 py-8 space-y-4">
        <h1 className="text-xl font-semibold">Tenant profiles</h1>
        <p className="text-sm text-ink-dim">
          Saved tenant credentials live in your browser's <code>localStorage</code>.
          Switch between tenants from the top-right selector on any page.
        </p>
        <div className="space-y-2">
          {profiles.length === 0 && (
            <div className="text-sm text-ink-dim border border-dashed border-border rounded-lg p-8 text-center">
              no tenant profiles yet — <Link to="/" className="text-accent underline">add one</Link>
            </div>
          )}
          {profiles.map((p) => (
            <div
              key={p.apiKey}
              className={`bg-bg-card border rounded-lg p-4 flex items-center justify-between ${
                active === p.apiKey ? "border-accent" : "border-border"
              }`}
            >
              <div className="space-y-1">
                <div className="font-medium">
                  {p.label} <span className="text-ink-dim text-sm">· {p.tenantId}</span>
                  {active === p.apiKey && (
                    <span className="ml-2 text-xs px-1.5 py-0.5 rounded bg-accent/20 text-accent">
                      active
                    </span>
                  )}
                </div>
                <div className="text-xs text-ink-dim font-mono">{p.baseUrl}</div>
                <button
                  onClick={() => setRevealed((r) => ({ ...r, [p.apiKey]: !r[p.apiKey] }))}
                  className="text-xs font-mono text-ink-dim hover:text-ink"
                >
                  {revealed[p.apiKey] ? p.apiKey : mask(p.apiKey)}
                </button>
              </div>
              <div className="flex gap-2">
                {active !== p.apiKey && (
                  <button
                    onClick={() => {
                      setActive(p.apiKey);
                      nav("/projects");
                    }}
                    className="text-sm px-3 py-1 rounded border border-border hover:border-accent"
                  >
                    switch
                  </button>
                )}
                <button
                  onClick={() => remove(p.apiKey)}
                  className="text-sm px-3 py-1 rounded border border-err/50 text-err hover:bg-err/10"
                >
                  remove
                </button>
              </div>
            </div>
          ))}
        </div>
      </main>
    </div>
  );
}
