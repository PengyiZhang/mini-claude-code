import { useEffect, useState } from "react";
import { Navigate, useNavigate } from "react-router-dom";
import { useAdmin } from "../lib/adminStore";
import { DEFAULT_BASE } from "../lib/api";

export default function AdminLogin() {
  const nav = useNavigate();
  const profile = useAdmin((s) => s.profile);
  const busy = useAdmin((s) => s.busy);
  const error = useAdmin((s) => s.error);
  const login = useAdmin((s) => s.login);
  const load = useAdmin((s) => s.load);

  const [baseUrl, setBaseUrl] = useState(DEFAULT_BASE);
  const [tenantId, setTenantId] = useState("");
  const [apiKey, setApiKey] = useState("");

  useEffect(() => {
    load();
  }, [load]);

  if (profile) return <Navigate to="/admin/keys" replace />;

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    const ok = await login(baseUrl, tenantId, apiKey);
    if (ok) nav("/admin/keys");
  }

  return (
    <div className="min-h-screen flex items-center justify-center px-6">
      <div className="w-full max-w-md space-y-6">
        <div className="text-center space-y-2">
          <div className="inline-flex items-center gap-2 text-2xl font-semibold tracking-tight">
            <div className="size-9 rounded-lg bg-gradient-to-br from-amber-400 to-rose-500 shadow-lg" />
            mini_cc · admin
          </div>
          <p className="text-sm text-ink-dim">
            tenant admin console — requires a key with <code className="font-mono">*</code> scope
          </p>
        </div>

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
          <Field label="Admin key">
            <input
              type="password"
              className="w-full bg-bg rounded px-3 py-2 border border-border focus:border-accent outline-none font-mono"
              value={apiKey}
              onChange={(e) => setApiKey(e.target.value)}
              placeholder="mck_... (must hold admin:read)"
              required
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
            className="w-full bg-amber-500 hover:bg-amber-600 disabled:opacity-50 text-white font-medium py-2 rounded"
          >
            {busy ? "verifying…" : "enter admin"}
          </button>
        </form>
        <p className="text-xs text-ink-faint text-center">
          generate an admin key with{" "}
          <code className="font-mono">python -m mini_cc.server keygen --scopes '*'</code>
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
