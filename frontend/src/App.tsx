import { useCallback, useEffect, useState } from "react";
import { Link, NavLink, Navigate, Route, Routes, useLocation } from "react-router-dom";
import { api, AuthStatus, setCsrfToken } from "./api";
import { LoginPage, SetupPage } from "./pages/Auth";
import LibraryPage from "./pages/Library";
import SlicePage from "./pages/Slice";
import { JobPage, JobsPage } from "./pages/Jobs";
import SettingsPage from "./pages/Settings";
import { PrinterProvider } from "./printers";
import { JobWatcher } from "./jobWatcher";
import { ToastProvider } from "./toast";
import { ConfirmProvider } from "./ui";
import { CertBanner } from "./certBanner";
import { Icon, IconName } from "./icons";

export default function App() {
  const location = useLocation();
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

  if (error)
    return (
      <div className="center card stack" role="alert">
        <h1>Can't reach HomeSlice</h1>
        <p className="muted">
          {!navigator.onLine
            ? "This device is offline. Reconnect to your home network and try again."
            : window.location.protocol === "https:"
              ? "The server didn't answer. If HomeSlice was just updated, its security certificate may have changed: reload the page and accept the browser's warning again (or install the HomeSlice certificate). Otherwise it may still be starting up."
              : "The server didn't answer. It may be restarting or updating; try again in a moment."}
        </p>
        <p className="muted small">Details: {error}</p>
        <div className="row">
          <button onClick={() => window.location.reload()}>Reload page</button>
          <button className="secondary" onClick={refresh}>
            Try again
          </button>
        </div>
      </div>
    );
  if (!status) return <div className="center muted">Loading…</div>;
  if (status.setup_required) return <SetupPage onDone={refresh} />;
  if (!status.authenticated) return <LoginPage onDone={refresh} />;
  if (location.pathname === "/orca") return <FullOrca />;

  return (
    <ToastProvider>
    <ConfirmProvider>
    <PrinterProvider>
    <JobWatcher />
    <a className="skip-link" href="#main">Skip to content</a>
    <div className="app">
      <header className="topbar">
        <span className="brand">HomeSlice</span>
        <Link
          className="button small secondary"
          to="/orca"
          title="Opens the complete OrcaSlicer desktop app in your browser, for editing presets and advanced plate work."
        >
          <Icon name="orca" size={16} />
          <span>
            <span className="wide-only">Advanced: </span>full OrcaSlicer
          </span>
        </Link>
      </header>
      <main className="content" id="main" tabIndex={-1}>
        <CertBanner />
        <Routes>
          <Route path="/" element={<Navigate to="/library" replace />} />
          <Route path="/login" element={<Navigate to="/library" replace />} />
          <Route path="/library" element={<LibraryPage />} />
          <Route path="/slice" element={<SlicePage />} />
          <Route path="/jobs" element={<JobsPage />} />
          <Route path="/jobs/:id" element={<JobSplit />} />
          <Route path="/settings" element={<SettingsPage username={status.username ?? ""} onLogout={refresh} />} />
          <Route path="*" element={<Navigate to="/library" replace />} />
        </Routes>
      </main>
      <nav className="tabbar" aria-label="Main">
        {NAV.map(([to, label, icon]) => (
          <NavLink key={to} to={to}>
            <span className="nav-icon">
              <Icon name={icon} size={22} />
            </span>
            {label}
          </NavLink>
        ))}
      </nav>
    </div>
    </PrinterProvider>
    </ConfirmProvider>
    </ToastProvider>
  );
}

const NAV: [string, string, IconName][] = [
  ["/library", "Library", "library"],
  ["/slice", "Slice", "slice"],
  ["/jobs", "Jobs", "jobs"],
  ["/settings", "Settings", "settings"],
];

/** On wide screens a job opens beside the list; on phones and tablets it takes the whole page. */
function JobSplit() {
  const wide = useMediaQuery("(min-width: 1024px)");
  if (!wide) return <JobPage />;
  return (
    <div className="split">
      <div className="split-list">
        <JobsPage compact />
      </div>
      <div className="split-detail">
        <JobPage />
      </div>
    </div>
  );
}

function useMediaQuery(q: string): boolean {
  const [match, setMatch] = useState(() => window.matchMedia(q).matches);
  useEffect(() => {
    const m = window.matchMedia(q);
    const on = () => setMatch(m.matches);
    m.addEventListener("change", on);
    return () => m.removeEventListener("change", on);
  }, [q]);
  return match;
}

function FullOrca() {
  return (
    <div className="orca-page">
      <header className="orca-bar">
        <Link className="button small secondary" to="/library">
          ← HomeSlice
        </Link>
        <span className="muted small">OrcaSlicer</span>
        <a className="button small secondary" href="/gui/" target="_blank" rel="noopener">
          New tab
        </a>
      </header>
      <iframe
        className="orca-frame"
        src="/gui/"
        title="OrcaSlicer"
        allow="clipboard-read; clipboard-write; fullscreen; autoplay"
      />
    </div>
  );
}
