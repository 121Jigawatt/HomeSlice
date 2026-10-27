import { useEffect, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { api, Job, Printer, PrinterStatus } from "../api";

const ACTIVE = new Set(["queued", "running"]);

function StatusBadge({ status }: { status: Job["status"] }) {
  return <span className={`badge ${status}`}>{status}</span>;
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
      {error && <p className="error">{error}</p>}
      {jobs.length === 0 && <p className="muted">No slicing jobs yet.</p>}
      <ul className="list">
        {jobs.map((j) => (
          <li key={j.id}>
            <Link to={`/jobs/${j.id}`} className="card row between plain">
              <div className="grow">
                <div className="title">{j.params.printer}</div>
                <div className="muted small">
                  {j.params.process} · {new Date(j.created_at * 1000).toLocaleString()}
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
      <div className="card stack small">
        <div>Printer: {job.params.printer}</div>
        <div>Process: {job.params.process}</div>
        <div>Filaments: {job.params.filaments.join(", ")}</div>
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
  const [state, setState] = useState<string | null>(null);
  const [start, setStart] = useState(false);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);

  useEffect(() => {
    api<Printer[]>("/api/printers")
      .then((list) => {
        setPrinters(list);
        setPrinterId((cur) => (list.some((p) => p.id === cur) ? cur : list[0]?.id ?? ""));
      })
      .catch((e) => setMsg({ ok: false, text: e.message }));
  }, []);

  useEffect(() => {
    setState(null);
    if (!printerId) return;
    api<PrinterStatus>(`/api/printers/${printerId}/status`)
      .then((s) => setState(s.state))
      .catch((e) => setState(`offline: ${e.message}`));
  }, [printerId]);

  if (gcodes.length === 0 || printers === null) return null;
  if (printers.length === 0)
    return (
      <p className="muted small">
        Add a printer in <Link to="/settings">Settings</Link> to send prints directly.
      </p>
    );

  async function send() {
    setBusy(true);
    setMsg(null);
    localStorage.setItem("send.printer", printerId);
    try {
      const r = await api<{ printer: string; filename: string; print_started: boolean }>(`/api/jobs/${job.id}/send`, {
        method: "POST",
        json: { printer_id: printerId, file, start },
      });
      setMsg({ ok: true, text: r.print_started ? `Printing on ${r.printer}.` : `Uploaded to ${r.printer} as ${r.filename}.` });
    } catch (e) {
      setMsg({ ok: false, text: (e as Error).message });
    } finally {
      setBusy(false);
    }
  }

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
      <div className="muted small">Status: {state ?? "checking…"}</div>
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
        <input type="checkbox" checked={start} onChange={(e) => setStart(e.target.checked)} /> Start printing now
      </label>
      {msg && <p className={msg.ok ? "muted" : "error"}>{msg.text}</p>}
      <button
        disabled={busy || !printerId || !file}
        onClick={() => (!start || confirm("Start printing now? Make sure the bed is clear.")) && send()}
      >
        {busy ? "Sending…" : start ? "Send & print" : "Send"}
      </button>
    </div>
  );
}
