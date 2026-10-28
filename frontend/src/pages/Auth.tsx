import { FormEvent, useState } from "react";
import { api, setCsrfToken } from "../api";

function safeNext(): string | null {
  const next = new URLSearchParams(window.location.search).get("next");
  // Same-origin relative paths only (prevents open redirects).
  return next && next.startsWith("/") && !next.startsWith("//") && !next.includes("\\") ? next : null;
}

export function LoginPage({ onDone }: { onDone: () => void }) {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const r = await api<{ csrf_token: string }>("/api/auth/login", { method: "POST", json: { username, password } });
      setCsrfToken(r.csrf_token);
      const next = safeNext();
      if (next?.startsWith("/gui/")) {
        window.location.assign(next);
        return;
      }
      if (next) window.history.replaceState(null, "", next);
      onDone();
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <form className="center card stack" onSubmit={submit}>
      <h1>HomeSlice</h1>
      <label>
        Username
        <input autoComplete="username" value={username} onChange={(e) => setUsername(e.target.value)} required />
      </label>
      <label>
        Password
        <input
          type="password"
          autoComplete="current-password"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          required
        />
      </label>
      {error && <p className="error">{error}</p>}
      <button disabled={busy}>{busy ? "Signing in…" : "Sign in"}</button>
    </form>
  );
}

export function SetupPage({ onDone }: { onDone: () => void }) {
  const [token, setToken] = useState("");
  const [username, setUsername] = useState("admin");
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [error, setError] = useState<string | null>(null);

  async function submit(e: FormEvent) {
    e.preventDefault();
    if (password !== confirm) {
      setError("Passwords do not match");
      return;
    }
    try {
      await api("/api/auth/setup", { method: "POST", json: { setup_token: token, username, password } });
      onDone();
    } catch (err) {
      setError((err as Error).message);
    }
  }

  return (
    <form className="center card stack" onSubmit={submit}>
      <h1>Welcome to HomeSlice</h1>
      <p className="muted">
        Enter the one-time setup token printed in the container log (search for “setup token”), then create your
        admin account.
      </p>
      <label>
        Setup token
        <input value={token} onChange={(e) => setToken(e.target.value)} autoComplete="off" required />
      </label>
      <label>
        Username
        <input value={username} onChange={(e) => setUsername(e.target.value)} autoComplete="username" required />
      </label>
      <label>
        Password (12+ characters)
        <input
          type="password"
          minLength={12}
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          autoComplete="new-password"
          required
        />
      </label>
      <label>
        Confirm password
        <input
          type="password"
          value={confirm}
          onChange={(e) => setConfirm(e.target.value)}
          autoComplete="new-password"
          required
        />
      </label>
      {error && <p className="error">{error}</p>}
      <button>Create account</button>
    </form>
  );
}

/** Shown after signing in with a password an admin set, until the user picks their own. */
export function ChangePasswordPage({ username, onDone }: { username: string; onDone: () => void }) {
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [confirm, setConfirm] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(e: FormEvent) {
    e.preventDefault();
    if (next !== confirm) {
      setError("Passwords do not match");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      await api("/api/auth/password", { method: "POST", json: { current_password: current, new_password: next } });
      onDone();
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <form className="center card stack" onSubmit={submit}>
      <h1>Choose your password</h1>
      <p className="muted">
        Welcome, <strong>{username}</strong>. You signed in with a temporary password; pick your own to continue.
      </p>
      <input type="text" hidden readOnly autoComplete="username" value={username} />
      <label>
        Temporary password
        <input type="password" autoComplete="current-password" value={current} onChange={(e) => setCurrent(e.target.value)} required />
      </label>
      <label>
        New password (12+ characters)
        <input type="password" autoComplete="new-password" minLength={12} value={next} onChange={(e) => setNext(e.target.value)} required />
      </label>
      <label>
        Confirm new password
        <input type="password" autoComplete="new-password" value={confirm} onChange={(e) => setConfirm(e.target.value)} required />
      </label>
      {error && <p className="error">{error}</p>}
      <button disabled={busy}>{busy ? "Saving…" : "Save and continue"}</button>
      <button type="button" className="secondary" onClick={() => api("/api/auth/logout", { method: "POST" }).finally(onDone)}>
        Sign out
      </button>
    </form>
  );
}
