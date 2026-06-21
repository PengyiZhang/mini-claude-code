import { useEffect } from "react";
import { Navigate, Outlet, useLocation } from "react-router-dom";
import { useAuth } from "./lib/store";
import { useAdmin } from "./lib/adminStore";

const ADMIN_PREFIX = "/admin";

export default function App() {
  const location = useLocation();
  const current = useAuth((s) => s.current());
  const load = useAuth((s) => s.load);
  const loadAdmin = useAdmin((s) => s.load);
  const isAdminRoute = location.pathname.startsWith(ADMIN_PREFIX);
  const adminProfile = useAdmin((s) => s.profile);
  const isLoginRoute = location.pathname === "/";

  useEffect(() => {
    load();
    loadAdmin();
  }, [load, loadAdmin]);

  // Admin routes have their own auth model — gated only by adminProfile.
  if (isAdminRoute) {
    if (!adminProfile && location.pathname !== "/admin/login") {
      return <Navigate to="/admin/login" replace />;
    }
    if (adminProfile && location.pathname === "/admin/login") {
      return <Navigate to="/admin/keys" replace />;
    }
    return <Outlet />;
  }

  if (!current && !isLoginRoute) {
    return <Navigate to="/" replace />;
  }
  if (current && isLoginRoute) {
    return <Navigate to="/projects" replace />;
  }
  return <Outlet />;
}
