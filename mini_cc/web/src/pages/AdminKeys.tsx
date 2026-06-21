import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { useAdmin } from "../lib/adminStore";
import { ApiError } from "../lib/api";
import type { KeyOut } from "../lib/types";

const SCOPE_TONES: Record<string, string> = {
  "*": "bg-rose-500/20 text-rose-300 border-rose-500/40",
  "admin:read": "bg-amber-500/20 text-amber-300 border-amber-500/40",
  "admin:write": "bg-orange-500/20 text-orange-300 border-orange-500/40",
  "read:*": "bg-sky-500/20 text-sky-300 border-sky-500/40",
  "write:*": "bg-violet-500/20 text-violet-300 border-violet-500/40",
};

function scopeTone(s: string): string {
  return SCOPE_TONES[s] ?? "bg-bg-hover text-ink-dim border-border";
}

function fmtDate(s: string | null): string {
  if (!s) return "—";
  try {
    return new Date(s).toLocaleString();
  } catch {
    return s;
  }
}

function fmtKey(k: string): string {
  // Show first 8 + last 4 chars; middle ellipsized.
  if (k.length <= 14) return k;
  return `${k.slice(0, 10)}…${k.slice(-4)}`;
}

export default function AdminKeys() {
  const profile = useAdmin((s) => s.profile);
  const keys = useAdmin((s) => s.keys);
  const busy = useAdmin((s) => s.busy);
  const error = useAdmin((s) => s.error);
  const refresh = useAdmin((s) => s.refresh);
  const logout = useAdmin((s) => s.logout);
  const createKey = useAdmin((s) => s.createKey);
  const patchKey = useAdmin((s) => s.patchKey);
  const revokeKey = useAdmin((s) => s.revokeKey);
  const rotateKey = useAdmin((s) => s.rotateKey);

  const [modal, setModal] = useState<null | {
    kind: "create" | "patch" | "rotate";
    target?: KeyOut;
  }>(null);

  useEffect(() => {
    void refresh();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  if (!profile) return null;

  return (
    <div className="min-h-screen flex flex-col">
      <header className="flex items-center justify-between border-b border-border bg-bg-panel/80 px-6 py-3 backdrop-blur">
        <Link to="/admin/keys" className="flex items-center gap-2">
          <div className="size-7 rounded-md bg-gradient-to-br from-amber-400 to-rose-500 shadow-inner" />
          <div className="font-semibold tracking-tight">
            mini_cc · admin <span className="text-ink-dim">/ keys</span>
          </div>
        </Link>
        <div className="flex items-center gap-3 text-sm">
          <span className="font-mono text-ink-dim">{profile.tenantId}</span>
          <Link
            to="/admin/metrics"
            className="px-3 py-1 rounded border border-border hover:border-accent hover:bg-bg-hover"
          >
            metrics
          </Link>
          <button
            onClick={() => refresh()}
            className="px-3 py-1 rounded border border-border hover:border-accent"
          >
            {busy ? "…" : "refresh"}
          </button>
          <button
            onClick={() => {
              if (confirm("log out of admin?")) logout();
            }}
            className="px-3 py-1 rounded border border-border hover:border-err text-err"
          >
            logout
          </button>
        </div>
      </header>

      <main className="flex-1 p-6 space-y-4">
        <div className="flex items-center justify-between">
          <h2 className="text-lg font-semibold">
            API keys <span className="text-sm text-ink-dim">({keys.length})</span>
          </h2>
          <button
            onClick={() => setModal({ kind: "create" })}
            className="px-4 py-1.5 bg-accent text-white rounded text-sm"
          >
            ＋ new key
          </button>
        </div>

        {error && (
          <div className="text-sm text-err bg-err/10 border border-err/40 rounded px-3 py-2">
            {error}
          </div>
        )}

        <div className="bg-bg-card border border-border rounded overflow-hidden">
          <table className="w-full text-sm">
            <thead className="bg-bg-panel text-ink-dim text-xs uppercase tracking-wide">
              <tr>
                <th className="text-left px-3 py-2">label</th>
                <th className="text-left px-3 py-2">key</th>
                <th className="text-left px-3 py-2">scopes</th>
                <th className="text-left px-3 py-2">created</th>
                <th className="text-left px-3 py-2">expires</th>
                <th className="text-left px-3 py-2">rotated from</th>
                <th className="text-right px-3 py-2">actions</th>
              </tr>
            </thead>
            <tbody>
              {keys.length === 0 && (
                <tr>
                  <td colSpan={7} className="px-3 py-6 text-center text-ink-dim">
                    no keys yet — click "new key" to create one
                  </td>
                </tr>
              )}
              {keys.map((k) => (
                <tr key={k.key} className="border-t border-border hover:bg-bg-hover">
                  <td className="px-3 py-2">{k.label || "—"}</td>
                  <td className="px-3 py-2 font-mono">{fmtKey(k.key)}</td>
                  <td className="px-3 py-2">
                    <div className="flex flex-wrap gap-1">
                      {k.scopes.map((s) => (
                        <span
                          key={s}
                          className={`px-1.5 py-0.5 rounded border text-xs ${scopeTone(s)}`}
                        >
                          {s}
                        </span>
                      ))}
                    </div>
                  </td>
                  <td className="px-3 py-2 text-ink-dim text-xs">{fmtDate(k.created_at)}</td>
                  <td className="px-3 py-2 text-ink-dim text-xs">{fmtDate(k.expires_at)}</td>
                  <td className="px-3 py-2 font-mono text-xs text-ink-faint">
                    {k.rotated_from ? fmtKey(k.rotated_from) : "—"}
                  </td>
                  <td className="px-3 py-2 text-right space-x-1">
                    <button
                      onClick={() => setModal({ kind: "patch", target: k })}
                      className="text-xs px-2 py-1 border border-border rounded hover:border-accent"
                    >
                      edit
                    </button>
                    <button
                      onClick={() => setModal({ kind: "rotate", target: k })}
                      className="text-xs px-2 py-1 border border-border rounded hover:border-accent"
                    >
                      rotate
                    </button>
                    <button
                      onClick={async () => {
                        if (!confirm(`revoke key "${k.label || k.key}"? this cannot be undone.`)) return;
                        try {
                          await revokeKey(k.key);
                        } catch (e) {
                          alert(e instanceof ApiError ? e.message : (e as Error).message);
                        }
                      }}
                      className="text-xs px-2 py-1 border border-err/40 text-err rounded hover:bg-err/10"
                    >
                      revoke
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </main>

      {modal && (
        <KeyModal
          kind={modal.kind}
          target={modal.target}
          onClose={() => setModal(null)}
          onSubmit={async (vals) => {
            try {
              if (modal.kind === "create") {
                await createKey(vals);
              } else if (modal.kind === "patch" && modal.target) {
                await patchKey(modal.target.key, vals);
              } else if (modal.kind === "rotate" && modal.target) {
                const grace = vals.grace_hours ?? 0;
                await rotateKey(modal.target.key, {
                  grace_hours: grace,
                  scopes: vals.scopes,
                  expires_in: vals.expires_in,
                  label: vals.label,
                });
              }
              setModal(null);
            } catch (e) {
              alert(e instanceof ApiError ? e.message : (e as Error).message);
            }
          }}
        />
      )}
    </div>
  );
}

function KeyModal({
  kind,
  target,
  onClose,
  onSubmit,
}: {
  kind: "create" | "patch" | "rotate";
  target?: KeyOut;
  onClose: () => void;
  onSubmit: (vals: {
    scopes?: string[];
    expires_in?: string;
    label?: string;
    grace_hours?: number;
  }) => Promise<void>;
}) {
  const [scopes, setScopes] = useState<string>((target?.scopes ?? ["read:*"]).join(", "));
  const [expiresIn, setExpiresIn] = useState<string>("");
  const [label, setLabel] = useState<string>(target?.label ?? "");
  const [graceHours, setGraceHours] = useState<number>(0);
  const [busy, setBusy] = useState(false);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    try {
      const parts = scopes.split(",").map((s) => s.trim()).filter(Boolean);
      await onSubmit({
        scopes: parts,
        expires_in: expiresIn.trim() || undefined,
        label: label.trim() || undefined,
        grace_hours: graceHours,
      });
    } finally {
      setBusy(false);
    }
  }

  const title =
    kind === "create" ? "new key" : kind === "patch" ? "edit key" : "rotate key";

  return (
    <div className="fixed inset-0 bg-black/50 flex items-center justify-center p-4">
      <form
        onSubmit={submit}
        className="bg-bg-card border border-border rounded-lg p-6 w-full max-w-md space-y-4"
      >
        <div className="text-lg font-semibold">{title}</div>

        {kind === "rotate" && (
          <>
            <div className="text-xs text-ink-dim">
              rotating <span className="font-mono">{target?.key}</span>
            </div>
            <Field label="Grace hours (0 = hard revoke old key)">
              <input
                type="number"
                min={0}
                className="w-full bg-bg rounded px-3 py-2 border border-border focus:border-accent outline-none"
                value={graceHours}
                onChange={(e) => setGraceHours(Number(e.target.value))}
              />
            </Field>
          </>
        )}

        <Field label="Scopes (comma-separated)">
          <input
            className="w-full bg-bg rounded px-3 py-2 border border-border focus:border-accent outline-none font-mono"
            value={scopes}
            onChange={(e) => setScopes(e.target.value)}
            placeholder="read:*, sessions:write, ..."
          />
        </Field>

        <Field label="Expires in (e.g. 7d, 24h, 30m) — blank = no expiry">
          <input
            className="w-full bg-bg rounded px-3 py-2 border border-border focus:border-accent outline-none font-mono"
            value={expiresIn}
            onChange={(e) => setExpiresIn(e.target.value)}
            placeholder="7d"
          />
        </Field>

        <Field label="Label (optional)">
          <input
            className="w-full bg-bg rounded px-3 py-2 border border-border focus:border-accent outline-none"
            value={label}
            onChange={(e) => setLabel(e.target.value)}
            placeholder="ci / mobile-app / ..."
          />
        </Field>

        <div className="flex justify-end gap-2 pt-2">
          <button
            type="button"
            onClick={onClose}
            className="px-3 py-1.5 border border-border rounded hover:bg-bg-hover"
          >
            cancel
          </button>
          <button
            type="submit"
            disabled={busy}
            className="px-3 py-1.5 bg-accent text-white rounded disabled:opacity-50"
          >
            {busy ? "…" : title}
          </button>
        </div>
      </form>
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
