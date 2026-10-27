import { useCallback, useEffect, useState } from "react";
import { NavLink, Navigate, Route, Routes } from "react-router-dom";
import { api, AuthStatus, setCsrfToken } from "./api";
import { LoginPage, SetupPage } from "./pages/Auth";
import LibraryPage from "./pages/Library";
import SlicePage from "./pages/Slice";
import { JobPage, JobsPage } from "./pages/Jobs";
import SettingsPage from "./pages/Settings";

export default function App() {
  const [status, setStatus] = useState<AuthStatus | null>(null);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      const s = await api<AuthStatus>("/api/auth/status");
      setCsrfToken(s.csrf_token);
      setStatus(s);
      setError(null);
    } catch (e) {
      setError((e as Error).message);
    }
  }, []);

  useEffect(() => {
    refresh();
    const onUnauthorized = () => refresh();
    window.addEventListener("homeslice:unauthorized", onUnauthorized);
    return () => window.removeEventListener("homeslice:unauthorized", onUnauthorized);
  }, [refresh]);

  if (error) return <div className="center card error">Cannot reach server: {error}</div>;
  if (!status) return <div className="center muted">Loading…</div>;
  if (status.setup_required) return <SetupPage onDone={refresh} />;
  if (!status.authenticated) return <LoginPage onDone={refresh} />;

  return (
    <div className="app">
      <header className="topbar">
        <span className="brand">HomeSlice</span>
        <a className="button small secondary" href="/gui/">
          Full Orca
        </a>
      </header>
      <main className="content">
        <Routes>
          <Route path="/" element={<Navigate to="/library" replace />} />
          <Route path="/login" element={<Navigate to="/library" replace />} />
          <Route path="/library" element={<LibraryPage />} />
          <Route path="/slice" element={<SlicePage />} />
          <Route path="/jobs" element={<JobsPage />} />
          <Route path="/jobs/:id" element={<JobPage />} />
          <Route path="/settings" element={<SettingsPage username={status.username ?? ""} onLogout={refresh} />} />
          <Route path="*" element={<Navigate to="/library" replace />} />
        </Routes>
      </main>
      <nav className="tabbar">
        <NavLink to="/library">Library</NavLink>
        <NavLink to="/slice">Slice</NavLink>
        <NavLink to="/jobs">Jobs</NavLink>
        <NavLink to="/settings">Settings</NavLink>
      </nav>
    </div>
  );
}
