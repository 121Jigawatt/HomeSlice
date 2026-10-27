import { useEffect, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { api, Job } from "../api";

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
