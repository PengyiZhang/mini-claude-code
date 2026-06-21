import { Link, useNavigate } from "react-router-dom";
import { useAuth } from "../lib/store";

export default function TopBar({ title }: { title?: string }) {
  const profile = useAuth((s) => s.current());
  const setActive = useAuth((s) => s.setActive);
  const profiles = useAuth((s) => s.profiles);
  const nav = useNavigate();
  return (
    <header className="flex items-center justify-between border-b border-border bg-bg-panel/80 px-6 py-3 backdrop-blur">
      <Link to="/projects" className="flex items-center gap-2 group">
        <div className="size-7 rounded-md bg-gradient-to-br from-accent to-indigo-400 shadow-inner" />
        <div className="font-semibold tracking-tight">mini_cc</div>
        {title && (
          <div className="ml-2 text-sm text-ink-dim group-hover:text-ink">/ {title}</div>
        )}
      </Link>
      <div className="flex items-center gap-3 text-sm">
        {profile && (
          <>
            <select
              className="bg-bg-card border border-border rounded px-2 py-1 text-ink hover:border-accent"
              value={profile.apiKey}
              onChange={(e) => {
                setActive(e.target.value);
                nav("/projects");
              }}
            >
              {profiles.map((p) => (
                <option key={p.apiKey} value={p.apiKey}>
                  {p.label} ({p.tenantId})
                </option>
              ))}
            </select>
            <Link
              to="/tenants"
              className="px-3 py-1 rounded border border-border hover:border-accent hover:bg-bg-hover"
            >
              manage tenants
            </Link>
            <Link
              to="/admin/login"
              className="px-3 py-1 rounded border border-amber-500/40 text-amber-300 hover:bg-amber-500/10"
            >
              admin
            </Link>
          </>
        )}
      </div>
    </header>
  );
}
