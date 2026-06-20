import { useEffect } from "react";
import { Navigate, Outlet, useLocation } from "react-router-dom";
import { useAuth } from "./lib/store";

export default function App() {
  const location = useLocation();
  const current = useAuth((s) => s.current());
  const load = useAuth((s) => s.load);
  const isLoginRoute = location.pathname === "/";

  useEffect(() => {
    load();
  }, [load]);

  if (!current && !isLoginRoute) {
    return <Navigate to="/" replace />;
  }
  if (current && isLoginRoute) {
    return <Navigate to="/projects" replace />;
  }
  return <Outlet />;
}
