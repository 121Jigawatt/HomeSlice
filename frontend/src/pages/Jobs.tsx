import { lazy, Suspense, useEffect, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { api, Job, Printer } from "../api";
import { ModelThumb } from "../ModelThumb";
import { formatDuration, IDLE_STATES, PrinterBadge, PrinterStrip, useLivePrinter } from "../printers";

const ACTIVE = new Set(["queued", "running"]);
const GcodeViewer = lazy(() => import("../viewer/GcodeViewer"));

function GcodePreview({ job }: { job: Job }) {
  const gcode = job.files.find((f) => f.endsWith(".gcode"));
  const [open, setOpen] = useState(false);
  if (!gcode) return null;
  return (
    <div className="card stack">
      <div className="row between">
        <strong>G-code preview</strong>
        <button type="button" className="small secondary" onClick={() => setOpen(!open)} aria-expanded={open}>
          {open ? "Hide" : "Show preview"}
        </button>
      </div>
      {open && (
        <Suspense fallback={<div className="viewer placeholder muted small">Loading viewer…</div>}>
          <GcodeViewer jobId={job.id} file={gcode} printerPreset={job.params.printer} />
        </Suspense>
      )}
    </div>
  );
}

function StatusBadge({ status }: { status: Job["status"] }) {
  return <span className={`badge ${status}`}>{status}</span>;
}

export function jobTitle(job: Job): string {
  const names = job.models.map((m) => m.filename ?? "(deleted model)");
  if (names.length === 0) return "Slice job";
  return names.length === 1 ? names[0] : `${names[0]} + ${names.length - 1} more`;
}

function JobThumb({ job, size }: { job: Job; size?: number }) {
  const m = job.models[0];
  return m ? <ModelThumb id={m.id} hasThumbnail={m.has_thumbnail} size={size} /> : null;
}

export function JobsPage() {
  const [jobs, setJobs] = useState<Job[]>([]);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let timer: number;
    const load = () =>
      api<Job[]>("/api/jobs")
        .then((j) => {
          setJobs(j);
          if (j.some((x) => ACTIVE.has(x.status))) timer = window.setTimeout(load, 3000);
        })
        .catch((e) => setError(e.message));
    load();
    return () => window.clearTimeout(timer);
  }, []);

  return (
    <section className="stack">
      <h2>Jobs</h2>
      <PrinterStrip />
      {error && <p className="error">{error}</p>}
      {jobs.length === 0 && <p className="muted">No slicing jobs yet.</p>}
      <ul className="list">
        {jobs.map((j) => (
          <li key={j.id}>
            <Link to={`/jobs/${j.id}`} className="card row between plain">
              <JobThumb job={j} />
              <div className="grow">
                <div className="title">{jobTitle(j)}</div>
                <div className="muted small">
                  {j.params.printer} · {j.params.process} · {new Date(j.created_at * 1000).toLocaleString()}
                </div>
              </div>
              <StatusBadge status={j.status} />
            </Link>
          </li>
        ))}
      </ul>
    </section>
  );
}

export function JobPage() {
  const { id } = useParams();
  const navigate = useNavigate();
  const [job, setJob] = useState<Job | null>(null);
  const [log, setLog] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let timer: number;
    const load = () =>
      api<Job>(`/api/jobs/${id}`)
        .then((j) => {
          setJob(j);
          if (ACTIVE.has(j.status)) timer = window.setTimeout(load, 2000);
        })
        .catch((e) => setError(e.message));
    load();
    return () => window.clearTimeout(timer);
  }, [id]);

  if (error) return <p className="error">{error}</p>;
  if (!job) return <p className="muted">Loading…</p>;

  async function cancel() {
    await api(`/api/jobs/${id}/cancel`, { method: "POST" }).catch((e) => setError(e.message));
  }

  async function remove() {
    if (!confirm("Delete this job and its output?")) return;
    await api(`/api/jobs/${id}`, { method: "DELETE" });
    navigate("/jobs");
  }

  return (
    <section className="stack">
      <div className="row between">
        <h2>Job</h2>
        <StatusBadge status={job.status} />
      </div>
      <div className="card row">
        <JobThumb job={job} size={96} />
        <div className="grow stack small">
          <div className="title">{jobTitle(job)}</div>
          <div>Printer: {job.params.printer}</div>
          <div>Process: {job.params.process}</div>
          <div>Filaments: {job.params.filaments.join(", ")}</div>
        </div>
      </div>
      {ACTIVE.has(job.status) && (
        <div className="card row between">
          <span>Slicing…</span>
          <button className="small secondary" onClick={cancel}>
            Cancel
          </button>
        </div>
      )}
      {job.error && <p className="error">{job.error}</p>}
      {job.stats && (
        <div className="card stack small">
          {job.stats.print_time && <div>Print time: {job.stats.print_time}</div>}
          {job.stats.filament_g && <div>Filament: {job.stats.filament_g} g</div>}
          {job.stats.filament_mm && <div>Filament length: {job.stats.filament_mm} mm</div>}
          {job.stats.layers && <div>Layers: {job.stats.layers}</div>}
        </div>
      )}
      {job.files.length > 0 && (
        <div className="card stack">
          <strong>Output</strong>
          {job.files.map((f) => (
            <a key={f} className="button secondary" href={`/api/jobs/${job.id}/files/${encodeURIComponent(f)}`}>
              ⬇ {f}
            </a>
          ))}
        </div>
      )}
      {job.status === "done" && <GcodePreview job={job} />}
      {job.status === "done" && <SendToPrinter job={job} />}
      <div className="row">
        <button className="small secondary" onClick={() => api<string>(`/api/jobs/${id}/log`).then(setLog)}>
          Show log
        </button>
        {!ACTIVE.has(job.status) && (
          <button className="small danger" onClick={remove}>
            Delete
          </button>
        )}
      </div>
      {log !== null && <pre className="log">{log || "(empty)"}</pre>}
    </section>
  );
}

function SendToPrinter({ job }: { job: Job }) {
  const gcodes = job.files.filter((f) => f.endsWith(".gcode"));
  const [printers, setPrinters] = useState<Printer[] | null>(null);
  const [printerId, setPrinterId] = useState(() => localStorage.getItem("send.printer") ?? "");
  const [file, setFile] = useState(gcodes[0] ?? "");
  const [start, setStart] = useState(false);
  const [confirming, setConfirming] = useState(false);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);
  const live = useLivePrinter(printerId);
  const canStart = !!live && live.connected && IDLE_STATES.has(live.state);

  useEffect(() => {
    api<Printer[]>("/api/printers")
      .then((list) => {
        setPrinters(list);
        setPrinterId((cur) => (list.some((p) => p.id === cur) ? cur : list[0]?.id ?? ""));
      })
      .catch((e) => setMsg({ ok: false, text: e.message }));
  }, []);

  // A printer that becomes busy while confirming must not receive a start.
  useEffect(() => {
    if (!canStart) setConfirming(false);
  }, [canStart]);

  if (gcodes.length === 0 || printers === null) return null;
  if (printers.length === 0)
    return (
      <p className="muted small">
        Add a printer in <Link to="/settings">Settings</Link> to send prints directly.
      </p>
    );

  async function send(startNow: boolean) {
    setBusy(true);
    setMsg(null);
    setConfirming(false);
    localStorage.setItem("send.printer", printerId);
    try {
      const r = await api<{ printer: string; filename: string; print_started: boolean }>(`/api/jobs/${job.id}/send`, {
        method: "POST",
        json: { printer_id: printerId, file, start: startNow },
      });
      setMsg({ ok: true, text: r.print_started ? `Printing on ${r.printer}.` : `Uploaded to ${r.printer} as ${r.filename}.` });
    } catch (e) {
      setMsg({ ok: false, text: (e as Error).message });
    } finally {
      setBusy(false);
    }
  }

  const blockedReason = !live
    ? "Checking printer…"
    : !live.connected
      ? `Printer is offline: ${live.error ?? "unreachable"}`
      : live.state === "printing" || live.state === "paused"
        ? `Printer is busy (${live.state}${live.filename ? `: ${live.filename}` : ""}).`
        : live.state === "error"
          ? `Printer reports an error${live.message ? `: ${live.message}` : ""}. Fix it on the printer first.`
          : null;

  return (
    <div className="card stack">
      <strong>Send to printer</strong>
      <label>
        Printer
        <select value={printerId} onChange={(e) => setPrinterId(e.target.value)}>
          {printers.map((p) => (
            <option key={p.id} value={p.id}>
              {p.name}
            </option>
          ))}
        </select>
      </label>
      <div className="row small" aria-live="polite">
        Status: {live ? <PrinterBadge state={live.state} /> : "checking…"}
        {live?.state === "printing" && live.remaining != null && (
          <span className="muted">{formatDuration(live.remaining)} left</span>
        )}
      </div>
      {gcodes.length > 1 && (
        <label>
          File
          <select value={file} onChange={(e) => setFile(e.target.value)}>
            {gcodes.map((f) => (
              <option key={f}>{f}</option>
            ))}
          </select>
        </label>
      )}
      <label className="row check">
        <input
          type="checkbox"
          checked={start && canStart}
          disabled={!canStart}
          onChange={(e) => setStart(e.target.checked)}
          aria-describedby="start-blocked"
        />{" "}
        Start printing now
      </label>
      {blockedReason && (
        <p id="start-blocked" className="muted small">
          {blockedReason} You can still upload the file without starting it.
        </p>
      )}
      {msg && <p className={msg.ok ? "muted" : "error"}>{msg.text}</p>}
      {confirming ? (
        <div className="confirm-box stack" role="alertdialog" aria-labelledby="confirm-start-title">
          <strong id="confirm-start-title">Start printing on {live?.name}?</strong>
          <p className="small">
            The printer will home and heat immediately. Make sure the bed is clear and the right filament is loaded.
          </p>
          <div className="row">
            <button onClick={() => send(true)} disabled={busy || !canStart} autoFocus>
              Yes, start printing
            </button>
            <button className="secondary" onClick={() => setConfirming(false)}>
              Cancel
            </button>
          </div>
        </div>
      ) : (
        <button
          disabled={busy || !printerId || !file || !live?.connected}
          onClick={() => (start && canStart ? setConfirming(true) : send(false))}
        >
          {busy ? "Sending…" : start && canStart ? "Send & print…" : "Upload to printer"}
        </button>
      )}
    </div>
  );
}
