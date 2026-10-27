import { FormEvent, useCallback, useEffect, useState } from "react";
import { api, OrcaStatus, Printer, PrinterStatus } from "../api";

export default function SettingsPage({ username, onLogout }: { username: string; onLogout: () => void }) {
  return (
    <section className="stack">
      <h2>Settings</h2>
      <Printers />
      <OrcaVersions />
      <PasswordForm />
      <div className="card row between">
        <span>
          Signed in as <strong>{username}</strong>
        </span>
        <button
          className="small secondary"
          onClick={() => api("/api/auth/logout", { method: "POST" }).finally(onLogout)}
        >
          Sign out
        </button>
      </div>
    </section>
  );
}

function Printers() {
  const [printers, setPrinters] = useState<Printer[]>([]);
  const [states, setStates] = useState<Record<string, string>>({});
  const [name, setName] = useState("");
  const [url, setUrl] = useState("");
  const [apiKey, setApiKey] = useState("");
  const [subnet, setSubnet] = useState("");
  const [subnetMsg, setSubnetMsg] = useState<{ ok: boolean; text: string } | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const list = await api<Printer[]>("/api/printers");
      setPrinters(list);
      for (const p of list) {
        api<PrinterStatus>(`/api/printers/${p.id}/status`)
          .then((s) => setStates((prev) => ({ ...prev, [p.id]: s.address ? `${s.state} @ ${s.address}` : s.state })))
          .catch((e) => setStates((prev) => ({ ...prev, [p.id]: `offline: ${e.message}` })));
      }
    } catch (e) {
      setError((e as Error).message);
    }
  }, []);

  useEffect(() => {
    load();
    api<{ scan_subnet: string | null }>("/api/printers/settings")
      .then((s) => setSubnet(s.scan_subnet ?? ""))
      .catch(() => undefined);
  }, [load]);

  async function saveSubnet(e: FormEvent) {
    e.preventDefault();
    setSubnetMsg(null);
    try {
      const s = await api<{ scan_subnet: string | null }>("/api/printers/settings", {
        method: "PUT",
        json: { scan_subnet: subnet || null },
      });
      setSubnet(s.scan_subnet ?? "");
      setSubnetMsg({ ok: true, text: "Saved." });
      load();
    } catch (err) {
      setSubnetMsg({ ok: false, text: (err as Error).message });
    }
  }

  async function add(e: FormEvent) {
    e.preventDefault();
    setError(null);
    try {
      await api("/api/printers", { method: "POST", json: { name, url, api_key: apiKey || null } });
      setName("");
      setUrl("");
      setApiKey("");
      load();
    } catch (err) {
      setError((err as Error).message);
    }
  }

  async function remove(p: Printer) {
    if (!confirm(`Remove ${p.name}?`)) return;
    await api(`/api/printers/${p.id}`, { method: "DELETE" }).catch((e) => setError(e.message));
    load();
  }

  return (
    <div className="card stack">
      <strong>Printers</strong>
      {printers.length === 0 && <p className="muted small">No printers yet. Add a Klipper (Mainsail/Fluidd) printer below.</p>}
      <ul className="list">
        {printers.map((p) => (
          <li key={p.id} className="row between small">
            <div className="grow">
              <div className="title">{p.name}</div>
              <div className="muted">
                {p.url} · {states[p.id] ?? "checking…"}
              </div>
            </div>
            <button className="small danger" onClick={() => remove(p)} aria-label={`Remove ${p.name}`}>
              ✕
            </button>
          </li>
        ))}
      </ul>
      <form className="stack" onSubmit={add}>
        <input placeholder="Name (e.g. SV08)" value={name} onChange={(e) => setName(e.target.value)} maxLength={64} required />
        <input
          placeholder="Address, e.g. http://spi-xi.local or http://10.69.69.165"
          value={url}
          onChange={(e) => setUrl(e.target.value)}
          inputMode="url"
          required
        />
        <input
          type="password"
          placeholder="Moonraker API key (optional)"
          value={apiKey}
          onChange={(e) => setApiKey(e.target.value)}
          autoComplete="off"
        />
        {error && <p className="error">{error}</p>}
        <button className="small">Add printer</button>
      </form>
      <form className="stack" onSubmit={saveSubnet}>
        <label>
          LAN range to search for printers by name
          <input placeholder="e.g. 10.69.69.0/24" value={subnet} onChange={(e) => setSubnet(e.target.value)} />
        </label>
        <p className="muted small">
          <code>.local</code> names don't resolve inside the container. With a range set, printers added by hostname are
          found by the name Klipper reports, so they keep working when their IP changes.
        </p>
        {subnetMsg && <p className={subnetMsg.ok ? "muted small" : "error"}>{subnetMsg.text}</p>}
        <button className="small secondary">Save range</button>
      </form>
    </div>
  );
}

function OrcaVersions() {
  const [status, setStatus] = useState<OrcaStatus | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [checking, setChecking] = useState(false);

  const load = useCallback(() => api<OrcaStatus>("/api/orca").then(setStatus).catch((e) => setError(e.message)), []);

  useEffect(() => {
    load();
  }, [load]);

  useEffect(() => {
    if (!status || status.progress.state === "idle") return;
    const t = window.setTimeout(load, 2000);
    return () => window.clearTimeout(t);
  }, [status, load]);

  if (!status) return <div className="card muted">{error ?? "Loading OrcaSlicer status…"}</div>;

  async function run(fn: () => Promise<unknown>) {
    setError(null);
    try {
      await fn();
    } catch (e) {
      setError((e as Error).message);
    }
    load();
  }

  const saveSettings = (channel: string, auto_update: boolean) =>
    run(() => api("/api/orca/settings", { method: "PUT", json: { channel, auto_update } }));

  const check = () =>
    run(async () => {
      setChecking(true);
      try {
        await api("/api/orca/check", { method: "POST", json: {} });
      } finally {
        setChecking(false);
      }
    });

  const busy = status.progress.state !== "idle";
  const latest = status.latest;
  const upToDate = latest && latest.version_id === status.current;

  return (
    <div className="card stack">
      <strong>OrcaSlicer engine</strong>
      <div className="small">
        Installed: <code>{status.current ?? "not installed"}</code>
      </div>
      <label>
        Update channel
        <select value={status.channel} onChange={(e) => saveSettings(e.target.value, status.auto_update)} disabled={busy}>
          <option value="stable">Stable releases</option>
          <option value="prerelease">Pre-releases (alpha/beta/RC)</option>
          <option value="nightly">Nightly builds (may be unstable)</option>
        </select>
      </label>
      <label className="row check">
        <input
          type="checkbox"
          checked={status.auto_update}
          onChange={(e) => saveSettings(status.channel, e.target.checked)}
        />
        Install updates automatically
      </label>
      <div className="row">
        <button className="small secondary" onClick={check} disabled={checking || busy}>
          {checking ? "Checking…" : "Check for updates"}
        </button>
        {status.previous && (
          <button
            className="small secondary"
            disabled={busy}
            onClick={() => confirm("Switch back to the previous version?") && run(() => api("/api/orca/rollback", { method: "POST" }))}
          >
            Roll back
          </button>
        )}
      </div>
      {latest && (
        <div className="stack small">
          <div>
            Latest on <em>{latest.channel}</em>: <strong>{latest.name || latest.tag}</strong>{" "}
            {latest.published_at && `(${new Date(latest.published_at).toLocaleDateString()})`}
          </div>
          {!latest.asset_sha256 && <div className="error">No published checksum — install will be refused.</div>}
          {upToDate ? (
            <div className="muted">You're up to date.</div>
          ) : (
            <button disabled={busy} onClick={() => run(() => api("/api/orca/install", { method: "POST", json: { channel: latest.channel } }))}>
              Install {latest.tag}
            </button>
          )}
          {latest.notes && (
            <details>
              <summary>Release notes</summary>
              <pre className="log">{latest.notes}</pre>
            </details>
          )}
        </div>
      )}
      {busy && (
        <div className="small">
          {status.progress.state}… {status.progress.version ?? ""}
        </div>
      )}
      {status.last_error && <p className="error">Last update failed: {status.last_error}</p>}
      {error && <p className="error">{error}</p>}
      {status.installed.length > 0 && (
        <details>
          <summary>Installed versions</summary>
          <ul className="list">
            {status.installed.map((v) => (
              <li key={v.id} className="row between small">
                <code>{v.id}</code>
                {v.current ? (
                  <span className="badge done">active</span>
                ) : (
                  <button className="small secondary" disabled={busy} onClick={() => run(() => api("/api/orca/activate", { method: "POST", json: { version_id: v.id } }))}>
                    Use
                  </button>
                )}
              </li>
            ))}
          </ul>
        </details>
      )}
    </div>
  );
}

function PasswordForm() {
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);

  async function submit(e: FormEvent) {
    e.preventDefault();
    try {
      await api("/api/auth/password", { method: "POST", json: { current_password: current, new_password: next } });
      setMsg({ ok: true, text: "Password changed. Other sessions were signed out." });
      setCurrent("");
      setNext("");
    } catch (err) {
      setMsg({ ok: false, text: (err as Error).message });
    }
  }

  return (
    <form className="card stack" onSubmit={submit}>
      <strong>Change password</strong>
      <input type="password" placeholder="Current password" autoComplete="current-password" value={current} onChange={(e) => setCurrent(e.target.value)} required />
      <input type="password" placeholder="New password (12+ characters)" autoComplete="new-password" minLength={12} value={next} onChange={(e) => setNext(e.target.value)} required />
      {msg && <p className={msg.ok ? "muted" : "error"}>{msg.text}</p>}
      <button className="small">Update password</button>
    </form>
  );
}
