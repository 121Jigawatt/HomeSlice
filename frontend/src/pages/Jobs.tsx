import { lazy, Suspense, useCallback, useEffect, useRef, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { api, Job, JobPage as JobPageData, Printer } from "../api";
import { Icon, IconName } from "../icons";
import { ModelThumb } from "../ModelThumb";
import { displayName } from "../presetNames";
import { BedFlag, formatDuration, IDLE_STATES, PrinterBadge, PrinterStrip, useLivePrinter } from "../printers";
import { PrintQueuePanel } from "../queue";
import { useToast } from "../toast";
import { OverflowMenu, useConfirm, useUndoableDelete } from "../ui";

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
  const icon: IconName = { queued: "queued", running: "printing", done: "done", failed: "error", cancelled: "offline" }[status] as IconName;
  const label = { queued: "Queued", running: "Slicing", done: "Done", failed: "Failed", cancelled: "Cancelled" }[status];
  return (
    <span className={`badge ${status}`}>
      <Icon name={icon} size={14} /> {label}
    </span>
  );
}

export function jobTitle(job: Job): string {
  if (job.title) return job.title;
  const names = job.models.map((m) => m.filename ?? "(deleted model)");
  if (names.length === 0) return "Slice job";
  return names.length === 1 ? names[0] : `${names[0]} + ${names.length - 1} more`;
}

function JobThumb({ job, size }: { job: Job; size?: number }) {
  const m = job.models[0];
  return m ? <ModelThumb id={m.id} hasThumbnail={m.has_thumbnail} size={size} /> : null;
}

const PAGE = 30;
/** Jobs deleted with an Undo window still open; hidden from the list until the delete commits. */
const hiddenJobs = new Set<string>();
const STATUS_FILTERS: [string, string][] = [
  ["", "All"],
  ["queued,running", "In progress"],
  ["done", "Done"],
  ["failed", "Failed"],
  ["cancelled", "Cancelled"],
];

function dayLabel(ts: number): string {
  const d = new Date(ts * 1000);
  const today = new Date();
  const start = (x: Date) => new Date(x.getFullYear(), x.getMonth(), x.getDate()).getTime();
  const diff = Math.round((start(today) - start(d)) / 86400000);
  if (diff === 0) return "Today";
  if (diff === 1) return "Yesterday";
  return d.toLocaleDateString(undefined, { weekday: "long", month: "short", day: "numeric", year: d.getFullYear() === today.getFullYear() ? undefined : "numeric" });
}

export function JobsPage({ compact = false }: { compact?: boolean }) {
  const { id: selectedId } = useParams();
  const [jobs, setJobs] = useState<Job[]>([]);
  const [total, setTotal] = useState(0);
  const [printersUsed, setPrintersUsed] = useState<string[]>([]);
  const [q, setQ] = useState("");
  const [status, setStatus] = useState("");
  const [printer, setPrinter] = useState("");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [debouncedQ, setDebouncedQ] = useState("");

  useEffect(() => {
    const t = window.setTimeout(() => setDebouncedQ(q.trim()), 250);
    return () => window.clearTimeout(t);
  }, [q]);

  const query = useCallback(
    (offset: number, limit = PAGE) => {
      const p = new URLSearchParams({ offset: String(offset), limit: String(limit) });
      if (debouncedQ) p.set("q", debouncedQ);
      if (status) p.set("status", status);
      if (printer) p.set("printer", printer);
      return api<JobPageData>(`/api/jobs?${p}`);
    },
    [debouncedQ, status, printer],
  );

  // Reload the first page on filter changes; keep refreshing while anything is in progress.
  useEffect(() => {
    let timer: number | undefined;
    let cancelled = false;
    const load = async () => {
      try {
        const page = await query(0, Math.max(PAGE, jobsRef.current.length));
        if (cancelled) return;
        setJobs(page.items);
        setTotal(page.total);
        setPrintersUsed(page.printers);
        setError(null);
        if (page.items.some((x) => ACTIVE.has(x.status))) timer = window.setTimeout(load, 3000);
      } catch (e) {
        if (!cancelled) setError((e as Error).message);
      } finally {
        if (!cancelled) setLoading(false);
      }
    };
    setLoading(true);
    load();
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
  }, [query]);

  const jobsRef = useRef<Job[]>([]);
  jobsRef.current = jobs;

  async function loadMore() {
    try {
      const page = await query(jobs.length);
      setJobs((j) => [...j, ...page.items]);
      setTotal(page.total);
    } catch (e) {
      setError((e as Error).message);
    }
  }

  const groups: [string, Job[]][] = [];
  for (const j of jobs) {
    if (hiddenJobs.has(j.id)) continue;
    const label = dayLabel(j.created_at);
    if (groups.length && groups[groups.length - 1][0] === label) groups[groups.length - 1][1].push(j);
    else groups.push([label, [j]]);
  }
  const filtered = !!(debouncedQ || status || printer);

  return (
    <section className="stack">
      {compact ? <h2>Jobs</h2> : <h1>Jobs</h1>}
      {!compact && <PrinterStrip />}
      {!compact && <PrintQueuePanel />}
      <div className="card stack filters" role="search">
        <label>
          Search by model name
          <input type="search" value={q} onChange={(e) => setQ(e.target.value)} placeholder="e.g. benchy" />
        </label>
        <div className="row">
          <label className="grow">
            Status
            <select value={status} onChange={(e) => setStatus(e.target.value)}>
              {STATUS_FILTERS.map(([value, label]) => (
                <option key={value} value={value}>
                  {label}
                </option>
              ))}
            </select>
          </label>
          <label className="grow">
            Printer preset
            <select value={printer} onChange={(e) => setPrinter(e.target.value)}>
              <option value="">All printers</option>
              {printersUsed.map((p) => (
                <option key={p} value={p} title={p}>
                  {displayName(p)}
                </option>
              ))}
            </select>
          </label>
        </div>
      </div>
      {error && <p className="error">{error}</p>}
      {!loading && jobs.length === 0 && (
        <div className="card empty">
          {filtered ? (
            <p className="muted">No jobs match these filters.</p>
          ) : (
            <>
              <p>No slicing jobs yet. Pick a model and printer settings to make your first one.</p>
              <Link className="button" to="/slice">
                Slice a model
              </Link>
            </>
          )}
        </div>
      )}
      {groups.map(([label, items]) => (
        <section key={label} className="stack" aria-label={label}>
          <h3 className="group-head">{label}</h3>
          <ul className="list">
            {items.map((j) => (
              <li key={j.id} className={`card job-row${j.id === selectedId ? " selected" : ""}`}>
                <Link to={`/jobs/${j.id}`} className="row plain grow job-link" aria-current={j.id === selectedId ? "page" : undefined}>
                  <JobThumb job={j} />
                  <div className="grow">
                    <div className="title">{jobTitle(j)}</div>
                    <div className="muted small" title={`${j.params.printer} \u00b7 ${j.params.process}`}>
                      {displayName(j.params.printer)} · {displayName(j.params.process)}
                    </div>
                    <div className="muted small">
                      {new Date(j.created_at * 1000).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })}
                      {j.stats?.print_time && ` \u00b7 ${j.stats.print_time}`}
                      {j.stats?.filament_g && ` \u00b7 ${j.stats.filament_g} g`}
                      {j.owner && ` \u00b7 by ${j.owner}`}
                    </div>
                  </div>
                  <StatusBadge status={j.status} />
                </Link>
                {!ACTIVE.has(j.status) && (
                  <Link className="button small secondary" to={`/slice?from=${j.id}`} aria-label={`Re-slice ${jobTitle(j)}`}>
                    Re-slice
                  </Link>
                )}
              </li>
            ))}
          </ul>
        </section>
      ))}
      {jobs.length < total && (
        <button className="secondary" onClick={loadMore}>
          Load more ({total - jobs.length} older)
        </button>
      )}
    </section>
  );
}

function ElapsedSince({ since }: { since: number | null }) {
  const [now, setNow] = useState(Date.now() / 1000);
  useEffect(() => {
    const t = window.setInterval(() => setNow(Date.now() / 1000), 1000);
    return () => window.clearInterval(t);
  }, []);
  return <>{since ? formatDuration(now - since) : "\u2013"}</>;
}

const STAGE_LABELS: Record<string, string> = {
  queued: "Waiting in queue",
  preparing: "Preparing presets",
  slicing: "Slicing",
  "arranging and slicing": "Arranging and slicing",
  "writing output": "Writing output",
};

function LogViewer({ jobId }: { jobId: string }) {
  const [log, setLog] = useState<string | null>(null);
  const firstError = useRef<HTMLSpanElement>(null);
  useEffect(() => {
    api<string>(`/api/jobs/${jobId}/log`)
      .then(setLog)
      .catch((e) => setLog(`Could not load log: ${e.message}`));
  }, [jobId]);
  useEffect(() => {
    firstError.current?.scrollIntoView({ block: "center" });
  }, [log]);
  if (log === null) return <p className="muted small">Loading log…</p>;
  const lines = (log || "(empty)").split("\n");
  const errorIndex = lines.findIndex((l) => /error|fail|exception|not compatible|invalid/i.test(l));
  return (
    <pre className="log" tabIndex={0} aria-label="Slicer log">
      {lines.map((l, i) =>
        i === errorIndex ? (
          <span key={i} ref={firstError} className="log-error">
            {l + "\n"}
          </span>
        ) : (
          l + "\n"
        ),
      )}
    </pre>
  );
}

export function JobPage() {
  const { id } = useParams();
  const navigate = useNavigate();
  const notify = useToast();
  const confirm = useConfirm();
  const undoableDelete = useUndoableDelete();
  const [job, setJob] = useState<Job | null>(null);
  const [showLog, setShowLog] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let timer: number;
    let cancelled = false;
    const load = () =>
      api<Job>(`/api/jobs/${id}`)
        .then((j) => {
          if (cancelled) return;
          setJob(j);
          if (ACTIVE.has(j.status)) timer = window.setTimeout(load, 1500);
        })
        .catch((e) => !cancelled && setError(e.message));
    load();
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
  }, [id]);

  if (error) return <p className="error">{error}</p>;
  if (!job) return <p className="muted">Loading…</p>;

  async function cancel() {
    await api(`/api/jobs/${id}/cancel`, { method: "POST" })
      .then(() => notify("Slice cancelled."))
      .catch((e) => setError(e.message));
  }

  async function retry() {
    try {
      const r = await api<{ id: string }>(`/api/jobs/${id}/retry`, { method: "POST" });
      notify("Retrying with the same settings.");
      navigate(`/jobs/${r.id}`);
    } catch (e) {
      notify(`Retry failed: ${(e as Error).message}`, { tone: "error" });
    }
  }

  async function remove() {
    const title = jobTitle(job!);
    const ok = await confirm({
      title: `Delete the job for ${title}?`,
      body: <p>Its G-code and log are removed. The model stays in the library.</p>,
      confirmLabel: "Delete job",
      danger: true,
    });
    if (!ok) return;
    const jobId = id!;
    undoableDelete(
      `the job for ${title}`,
      () => {
        hiddenJobs.add(jobId);
        navigate("/jobs");
      },
      () => hiddenJobs.delete(jobId),
      () => api(`/api/jobs/${jobId}`, { method: "DELETE" }).finally(() => hiddenJobs.delete(jobId)),
    );
  }

  const info = job.error_info;
  return (
    <section className="stack">
      <div className="row between">
        <h1 className="grow title">{jobTitle(job)}</h1>
        <StatusBadge status={job.status} />
      </div>
      <div className="card row">
        <JobThumb job={job} size={96} />
        <div className="grow stack small">
          <div className="title">{jobTitle(job)}</div>
          <div title={job.params.printer}>Printer: {displayName(job.params.printer)}</div>
          <div title={job.params.process}>Process: {displayName(job.params.process)}</div>
          <div>Filaments: {job.params.filaments.map(displayName).join(", ")}</div>
          {job.owner && <div className="muted">Sliced by {job.owner}</div>}
        </div>
      </div>
      {ACTIVE.has(job.status) && (
        <div className="card stack" aria-live="polite">
          <div className="row between">
            <strong>{STAGE_LABELS[job.stage ?? job.status] ?? job.stage ?? "Working"}…</strong>
            <span className="muted small">
              {job.status === "running" ? <ElapsedSince since={job.started_at} /> : null}
            </span>
          </div>
          <div className="progress-bar indeterminate" role="progressbar" aria-label="Slicing in progress">
            <span />
          </div>
          <button className="small secondary" onClick={cancel}>
            Cancel slice
          </button>
        </div>
      )}
      {job.status === "failed" && info && (
        <div className="card stack error-card" role="alert">
          <strong>
            <Icon name="error" /> {info.title}
          </strong>
          <p className="small">
            <b>Likely cause:</b> {info.cause}
          </p>
          <p className="small">
            <b>Try:</b> {info.fix}
          </p>
          <div className="row">
            <button onClick={retry}>Retry</button>
            <Link className="button secondary" to={`/slice?from=${job.id}`}>
              Edit settings and re-slice
            </Link>
          </div>
          <details onToggle={(e) => setShowLog((e.target as HTMLDetailsElement).open)}>
            <summary>Technical details</summary>
            <p className="small muted">
              {info.code != null && <>Code {info.code} · </>}
              {info.raw}
            </p>
            {showLog && <LogViewer jobId={job.id} />}
          </details>
        </div>
      )}
      {job.status === "cancelled" && (
        <div className="card row between">
          <span className="muted">This slice was cancelled.</span>
          <button className="small" onClick={retry}>
            Slice again
          </button>
        </div>
      )}
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
            <a key={f} className="button secondary" href={`/api/jobs/${job.id}/files/${encodeURIComponent(f)}`} download>
              <Icon name="download" size={16} /> Download {f}
            </a>
          ))}
        </div>
      )}
      {job.status === "done" && <GcodePreview job={job} />}
      {job.status === "done" && <SendToPrinter job={job} />}
      {job.status !== "failed" && !ACTIVE.has(job.status) && (
        <details className="card">
          <summary>Technical details</summary>
          <LogViewer jobId={job.id} />
        </details>
      )}
      <div className="row between">
        {!ACTIVE.has(job.status) && (
          <Link className="button small secondary" to={`/slice?from=${job.id}`}>
            Re-slice with these settings
          </Link>
        )}
        {!ACTIVE.has(job.status) && (
          <OverflowMenu
            label={`More actions for this job`}
            items={[{ label: "Delete job…", icon: "trash", danger: true, onSelect: remove }]}
          />
        )}
      </div>
    </section>
  );
}

function SendToPrinter({ job }: { job: Job }) {
  const notify = useToast();
  const gcodes = job.files.filter((f) => f.endsWith(".gcode"));
  const [printers, setPrinters] = useState<Printer[] | null>(null);
  const [printerId, setPrinterId] = useState(() => localStorage.getItem("send.printer") ?? "");
  const [file, setFile] = useState(gcodes[0] ?? "");
  const [start, setStart] = useState(false);
  const [confirming, setConfirming] = useState(false);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);
  const live = useLivePrinter(printerId);
  const canStart = !!live && live.connected && IDLE_STATES.has(live.state) && !live.needs_attention;

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
      notify(r.print_started ? `Print started on ${r.printer}.` : `Sent to ${r.printer}.`, { tone: "success" });
    } catch (e) {
      setMsg({ ok: false, text: (e as Error).message });
      notify(`Send failed: ${(e as Error).message}`, { tone: "error" });
    } finally {
      setBusy(false);
    }
  }

  async function enqueue() {
    setBusy(true);
    setMsg(null);
    localStorage.setItem("send.printer", printerId);
    try {
      await api("/api/queue", { method: "POST", json: { job_id: job.id, printer_id: printerId, file } });
      const name = printers?.find((p) => p.id === printerId)?.name ?? "the printer";
      notify(`Queued for ${name}. It starts when ${name} is idle and its bed is confirmed clear.`, {
        tone: "success",
        action: { label: "View queue", to: "/jobs#queue" },
      });
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
          : live.needs_attention
            ? "Confirm the bed is clear before starting."
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
      {live && <BedFlag p={live} />}
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
        <div className="row">
          <button
            className="grow"
            disabled={busy || !printerId || !file || !live?.connected}
            onClick={() => (start && canStart ? setConfirming(true) : send(false))}
          >
            {busy ? "Sending…" : start && canStart ? "Send & print…" : "Upload to printer"}
          </button>
          <button
            className="secondary grow"
            disabled={busy || !printerId || !file}
            onClick={enqueue}
            title="Prints automatically once this printer is idle and you've confirmed its bed is clear."
          >
            Add to queue
          </button>
        </div>
      )}
    </div>
  );
}
