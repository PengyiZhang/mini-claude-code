import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { useAuth } from "../lib/store";
import { ApiError, DEFAULT_BASE, verifyKey } from "../lib/api";

export default function Login() {
  const nav = useNavigate();
  const add = useAuth((s) => s.add);
  const load = useAuth((s) => s.load);
  const profiles = useAuth((s) => s.profiles);

  const [baseUrl, setBaseUrl] = useState(DEFAULT_BASE);
  const [apiKey, setApiKey] = useState("");
  const [tenantId, setTenantId] = useState("");
  const [label, setLabel] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    load();
  }, [load]);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    setBusy(true);
    try {
      await verifyKey(baseUrl.trim(), apiKey.trim(), tenantId.trim());
      add({
        baseUrl: baseUrl.trim(),
        apiKey: apiKey.trim(),
        tenantId: tenantId.trim(),
        label: label.trim() || tenantId.trim(),
      });
      nav("/projects");
    } catch (e) {
      if (e instanceof ApiError) setError(`${e.code}: ${e.message}`);
      else setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="min-h-screen flex items-center justify-center px-6">
      <div className="w-full max-w-md space-y-6">
        <div className="text-center space-y-2">
          <div className="inline-flex items-center gap-2 text-2xl font-semibold tracking-tight">
            <div className="size-9 rounded-lg bg-gradient-to-br from-accent to-indigo-400 shadow-lg" />
            mini_cc
          </div>
          <p className="text-sm text-ink-dim">
            multi-tenant agent console — sign in with an API key
          </p>
        </div>

        {profiles.length > 0 && (
          <div className="bg-bg-card border border-border rounded-lg p-4 text-sm space-y-2">
            <div className="text-ink-dim text-xs uppercase tracking-wide">saved tenants</div>
            <div className="flex flex-wrap gap-2">
              {profiles.map((p) => (
                <button
                  key={p.apiKey}
                  onClick={() => {
                    setBaseUrl(p.baseUrl);
                    setApiKey(p.apiKey);
                    setTenantId(p.tenantId);
                    setLabel(p.label);
                  }}
                  className="px-2 py-1 rounded border border-border hover:border-accent hover:bg-bg-hover"
                >
                  {p.label}
                </button>
              ))}
            </div>
          </div>
        )}

        <form onSubmit={submit} className="space-y-4 bg-bg-card border border-border rounded-lg p-6">
          <Field label="Base URL">
            <input
              className="w-full bg-bg rounded px-3 py-2 border border-border focus:border-accent outline-none"
              value={baseUrl}
              onChange={(e) => setBaseUrl(e.target.value)}
              placeholder={DEFAULT_BASE}
            />
          </Field>
          <Field label="Tenant ID">
            <input
              className="w-full bg-bg rounded px-3 py-2 border border-border focus:border-accent outline-none font-mono"
              value={tenantId}
              onChange={(e) => setTenantId(e.target.value)}
              placeholder="tenant1"
              required
            />
          </Field>
          <Field label="API key">
            <input
              type="password"
              className="w-full bg-bg rounded px-3 py-2 border border-border focus:border-accent outline-none font-mono"
              value={apiKey}
              onChange={(e) => setApiKey(e.target.value)}
              placeholder="mck_..."
              required
            />
          </Field>
          <Field label="Label (optional)">
            <input
              className="w-full bg-bg rounded px-3 py-2 border border-border focus:border-accent outline-none"
              value={label}
              onChange={(e) => setLabel(e.target.value)}
              placeholder="prod / dev / local"
            />
          </Field>
          {error && (
            <div className="text-sm text-err bg-err/10 border border-err/40 rounded px-3 py-2">
              {error}
            </div>
          )}
          <button
            type="submit"
            disabled={busy}
            className="w-full bg-accent hover:bg-accent-hover disabled:opacity-50 text-white font-medium py-2 rounded"
          >
            {busy ? "verifying…" : "sign in"}
          </button>
        </form>
        <p className="text-xs text-ink-faint text-center">
          generate a key with <code className="font-mono">python -m mini_cc.server keygen &lt;tenant&gt;</code>
        </p>
      </div>
    </div>
  );
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <label className="block space-y-1.5">
      <div className="text-xs uppercase tracking-wide text-ink-dim">{label}</div>
      {children}
    </label>
  );
}
