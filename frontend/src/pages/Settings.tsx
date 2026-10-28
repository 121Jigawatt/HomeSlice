import { FormEvent, useCallback, useEffect, useState } from "react";
import { api, OrcaStatus, Printer, PrinterStatus } from "../api";
import { useToast } from "../toast";
import { OverflowMenu, useConfirm, useUndoableDelete } from "../ui";

export default function SettingsPage({ username, onLogout }: { username: string; onLogout: () => void }) {
  return (
    <section className="stack">
      <h1>Settings</h1>
      <Printers />
      <OrcaVersions />
      <JobCleanup />
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

  const confirm = useConfirm();
  const undoableDelete = useUndoableDelete();
  async function remove(p: Printer) {
    const ok = await confirm({
      title: `Remove ${p.name}?`,
      body: <p>HomeSlice stops showing its status and can no longer send prints to it. Nothing on the printer changes.</p>,
      confirmLabel: "Remove printer",
      danger: true,
    });
    if (!ok) return;
    undoableDelete(
      p.name,
      () => setPrinters((ps) => ps.filter((x) => x.id !== p.id)),
      load,
      () => api(`/api/printers/${p.id}`, { method: "DELETE" }).then(load),
    );
  }

  return (
    <div className="card stack">
      <h2>Printers</h2>
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
            <OverflowMenu
              label={`More actions for ${p.name}`}
              items={[{ label: "Remove printer…", icon: "trash", danger: true, onSelect: () => remove(p) }]}
            />
          </li>
        ))}
      </ul>
      <form className="stack form-grid" onSubmit={add} aria-label="Add a printer">
        <label>
          Name
          <input placeholder="e.g. SV08" value={name} onChange={(e) => setName(e.target.value)} maxLength={64} required />
        </label>
        <label>
          Address
          <input
            placeholder="http://spi-xi.local or http://10.69.69.165"
            value={url}
            onChange={(e) => setUrl(e.target.value)}
            inputMode="url"
            required
          />
        </label>
        <label className="span-2">
          Moonraker API key (optional)
          <input type="password" value={apiKey} onChange={(e) => setApiKey(e.target.value)} autoComplete="off" />
        </label>
        {error && <p className="error span-2">{error}</p>}
        <button className="small span-2">Add printer</button>
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
  const confirm = useConfirm();
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
      <h2>OrcaSlicer engine</h2>
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
            onClick={async () => {
              const ok = await confirm({
                title: `Switch back to OrcaSlicer ${status.previous}?`,
                body: <p>New slices use the previous version. You can switch forward again from Installed versions.</p>,
                confirmLabel: "Roll back",
              });
              if (ok) run(() => api("/api/orca/rollback", { method: "POST" }));
            }}
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

function JobCleanup() {
  const notify = useToast();
  const confirm = useConfirm();
  const [days, setDays] = useState(30);
  const [includeFailed, setIncludeFailed] = useState(true);
  const [busy, setBusy] = useState(false);

  async function run(e: FormEvent) {
    e.preventDefault();
    const what = includeFailed ? "finished, failed and cancelled" : "finished";
    const ok = await confirm({
      title: `Delete ${what} jobs older than ${days} days?`,
      body: <p>Their G-code and logs are removed permanently. This can't be undone. Models stay in the library.</p>,
      confirmLabel: "Delete old jobs",
      danger: true,
    });
    if (!ok) return;
    setBusy(true);
    try {
      const r = await api<{ removed: number }>("/api/jobs/cleanup", {
        method: "POST",
        json: { older_than_days: days, include_failed: includeFailed },
      });
      notify(r.removed ? `Removed ${r.removed} old job${r.removed === 1 ? "" : "s"}.` : "Nothing to clean up.", {
        tone: "success",
      });
    } catch (err) {
      notify(`Cleanup failed: ${(err as Error).message}`, { tone: "error" });
    } finally {
      setBusy(false);
    }
  }

  return (
    <form className="card stack" onSubmit={run}>
      <h2>Job history</h2>
      <label>
        Clear completed jobs older than (days)
        <input type="number" min={0} max={3650} value={days} onChange={(e) => setDays(Number(e.target.value))} required />
      </label>
      <label className="row check">
        <input type="checkbox" checked={includeFailed} onChange={(e) => setIncludeFailed(e.target.checked)} /> Include
        failed and cancelled jobs
      </label>
      <button className="small secondary" disabled={busy}>
        {busy ? "Clearing…" : "Clear old jobs"}
      </button>
    </form>
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
      <h2>Change password</h2>
      <input type="password" placeholder="Current password" autoComplete="current-password" value={current} onChange={(e) => setCurrent(e.target.value)} required />
      <input type="password" placeholder="New password (12+ characters)" autoComplete="new-password" minLength={12} value={next} onChange={(e) => setNext(e.target.value)} required />
      {msg && <p className={msg.ok ? "muted" : "error"}>{msg.text}</p>}
      <button className="small">Update password</button>
    </form>
  );
}
