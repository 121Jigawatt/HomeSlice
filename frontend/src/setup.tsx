import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api, JobPage, OrcaStatus, Printer } from "./api";
import { Icon } from "./icons";

const DISMISS_KEY = "setup.dismissed";

interface Step {
  done: boolean;
  title: string;
  hint: string;
  action?: { label: string; to: string };
  optional?: boolean;
}

/** First-run checklist on the Library page; disappears once everything is set up or when dismissed. */
export function GettingStarted({ modelCount }: { modelCount: number | null }) {
  const [dismissed, setDismissed] = useState(() => localStorage.getItem(DISMISS_KEY) === "1");
  const [state, setState] = useState<{ orca: OrcaStatus; presets: number; printers: number; jobs: number } | null>(null);

  useEffect(() => {
    if (dismissed) return;
    let stopped = false;
    const load = async () => {
      try {
        const [orca, presets, printers, jobs] = await Promise.all([
          api<OrcaStatus>("/api/orca"),
          api<unknown[]>("/api/profiles/printer"),
          api<Printer[]>("/api/printers"),
          api<JobPage>("/api/jobs?limit=1"),
        ]);
        if (!stopped) setState({ orca, presets: presets.length, printers: printers.length, jobs: jobs.total });
      } catch {
        /* The checklist is a convenience; leave it hidden if anything fails. */
      }
    };
    load();
    // Refresh while OrcaSlicer installs in the background.
    const t = window.setInterval(load, 10000);
    return () => {
      stopped = true;
      window.clearInterval(t);
    };
  }, [dismissed]);

  if (dismissed || !state || modelCount === null) return null;
  const installing = state.orca.progress.state !== "idle";
  const steps: Step[] = [
    {
      done: !!state.orca.current,
      title: "Slicing engine installed",
      hint: installing
        ? `Installing OrcaSlicer (${state.orca.progress.state})… this takes a few minutes the first time.`
        : state.orca.last_error
          ? `Install failed: ${state.orca.last_error}`
          : "HomeSlice downloads OrcaSlicer automatically on first start.",
      action: installing ? undefined : { label: "Open settings", to: "/settings" },
    },
    {
      done: state.presets > 0,
      title: "Printer profiles ready",
      hint: "Set up your printer, process and filament presets once in the full OrcaSlicer; HomeSlice uses them for every slice.",
      action: { label: "Open full OrcaSlicer", to: "/orca" },
    },
    {
      done: state.printers > 0,
      title: "Connect a printer",
      hint: "Add a Klipper printer (Mainsail or Fluidd) to see its status and send prints directly.",
      action: { label: "Add printer", to: "/settings" },
      optional: true,
    },
    {
      done: modelCount > 0,
      title: "Add a model",
      hint: "Upload or drop an STL, 3MF, OBJ or STEP file, or import from a Printables or MakerWorld link.",
    },
    {
      done: state.jobs > 0,
      title: "Slice it",
      hint: "Pick the model, printer, process and filament; HomeSlice checks they fit together before slicing.",
      action: { label: "Go to Slice", to: "/slice" },
    },
  ];
  const remaining = steps.filter((s) => !s.done).length;
  if (remaining === 0) return null;

  return (
    <section className="card stack getting-started" aria-labelledby="setup-title">
      <div className="row between">
        <h2 id="setup-title">Getting started</h2>
        <span className="muted small">
          {steps.length - remaining} of {steps.length} done
        </span>
      </div>
      <ol className="list setup-steps">
        {steps.map((s) => (
          <li key={s.title} className={`row setup-step${s.done ? " done" : ""}`}>
            <span className="setup-check" aria-hidden="true">
              {s.done ? <Icon name="done" size={16} /> : null}
            </span>
            <div className="grow">
              <div className="title">
                {s.title}
                {s.optional && <span className="muted small"> (optional)</span>}
                <span className="sr-only">{s.done ? " – done" : " – to do"}</span>
              </div>
              {!s.done && <div className="muted small">{s.hint}</div>}
            </div>
            {!s.done && s.action && (
              <Link className="button small secondary" to={s.action.to}>
                {s.action.label}
              </Link>
            )}
          </li>
        ))}
      </ol>
      <button
        type="button"
        className="small secondary setup-dismiss"
        onClick={() => {
          localStorage.setItem(DISMISS_KEY, "1");
          setDismissed(true);
        }}
      >
        Hide this checklist
      </button>
    </section>
  );
}
