import { createContext, ReactNode, useContext, useEffect, useRef, useState } from "react";
import { api, LivePrinter } from "./api";
import { Icon, IconName } from "./icons";

const POLL_MS = 3000;

type LiveState = { printers: LivePrinter[] | null; error: string | null };
const PrinterContext = createContext<LiveState>({ printers: null, error: null });

/** One shared poll for the whole app; paused while the tab is hidden. */
export function PrinterProvider({ children }: { children: ReactNode }) {
  const [state, setState] = useState<LiveState>({ printers: null, error: null });

  useEffect(() => {
    let timer: number | undefined;
    let stopped = false;
    const tick = async () => {
      if (document.visibilityState === "visible") {
        try {
          const printers = await api<LivePrinter[]>("/api/printers/live");
          if (!stopped) setState({ printers, error: null });
        } catch (e) {
          if (!stopped) setState((s) => ({ ...s, error: (e as Error).message }));
        }
      }
      if (!stopped) timer = window.setTimeout(tick, POLL_MS);
    };
    tick();
    const onVisible = () => {
      if (document.visibilityState === "visible") {
        window.clearTimeout(timer);
        tick();
      }
    };
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      stopped = true;
      window.clearTimeout(timer);
      document.removeEventListener("visibilitychange", onVisible);
    };
  }, []);

  return <PrinterContext.Provider value={state}>{children}</PrinterContext.Provider>;
}

export function useLivePrinters(): LiveState {
  return useContext(PrinterContext);
}

export function useLivePrinter(id: string | null | undefined): LivePrinter | undefined {
  const { printers } = useLivePrinters();
  return printers?.find((p) => p.id === id);
}

const STATE_INFO: Record<string, { icon: IconName; label: string; tone: string }> = {
  printing: { icon: "printing", label: "Printing", tone: "running" },
  paused: { icon: "paused", label: "Paused", tone: "warn" },
  standby: { icon: "ready", label: "Idle", tone: "done" },
  complete: { icon: "done", label: "Finished", tone: "done" },
  cancelled: { icon: "ready", label: "Idle (cancelled)", tone: "done" },
  error: { icon: "error", label: "Error", tone: "failed" },
  offline: { icon: "offline", label: "Offline", tone: "failed" },
};

export function stateInfo(state: string) {
  return STATE_INFO[state] ?? { icon: "ready" as IconName, label: state, tone: "" };
}

/** States in which a new print may be started. */
export const IDLE_STATES = new Set(["standby", "complete", "cancelled"]);

export function PrinterBadge({ state }: { state: string }) {
  const info = stateInfo(state);
  return (
    <span className={`badge ${info.tone}`}>
      <Icon name={info.icon} size={14} /> {info.label}
    </span>
  );
}

export function formatDuration(seconds: number | null | undefined): string {
  if (seconds == null || !isFinite(seconds)) return "–";
  const s = Math.max(0, Math.round(seconds));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  return h ? `${h}h ${m}m` : m ? `${m}m` : `${s}s`;
}

function temp(t: { temp: number | null; target: number | null } | null | undefined): string {
  if (!t || t.temp == null) return "–";
  return t.target ? `${Math.round(t.temp)}/${Math.round(t.target)}°` : `${Math.round(t.temp)}°`;
}

function PrinterCard({ p }: { p: LivePrinter }) {
  const [open, setOpen] = useState(false);
  const active = p.state === "printing" || p.state === "paused";
  const pct = p.progress != null ? Math.round(p.progress * 100) : null;
  return (
    <li className={`printer-card${open ? " open" : ""}`}>
      <button
        type="button"
        className="printer-head plain"
        aria-expanded={open}
        onClick={() => setOpen(!open)}
      >
        <span className="printer-name">{p.name}</span>
        <PrinterBadge state={p.state} />
      </button>
      {active && (
        <div className="printer-job small">
          <div className="title" title={p.filename ?? ""}>
            {p.filename}
          </div>
          <div className="progress-bar" role="progressbar" aria-valuenow={pct ?? 0} aria-valuemin={0} aria-valuemax={100} aria-label={`${p.name} progress`}>
            <span style={{ width: `${pct ?? 0}%` }} />
          </div>
          <div className="muted">
            {pct ?? 0}% · {formatDuration(p.elapsed)} elapsed · {formatDuration(p.remaining)} left
          </div>
        </div>
      )}
      {p.connected ? (
        <div className="muted small">
          Nozzle {temp(p.extruder)} · Bed {temp(p.bed)}
        </div>
      ) : (
        <div className="muted small" title={p.error ?? ""}>
          {p.error}
        </div>
      )}
      {open && p.connected && (
        <div className="stack small">
          {p.message && <div className="error">{p.message}</div>}
          {p.webcams.length > 0 ? (
            p.webcams.map((name, i) => <Webcam key={name + i} printerId={p.id} index={i} name={name} />)
          ) : (
            <div className="muted">No webcam configured.</div>
          )}
        </div>
      )}
    </li>
  );
}

function Webcam({ printerId, index, name }: { printerId: string; index: number; name: string }) {
  const [live, setLive] = useState(false);
  const [tick, setTick] = useState(0);
  const imgRef = useRef<HTMLImageElement>(null);

  useEffect(() => {
    if (live) return;
    const t = window.setInterval(() => document.visibilityState === "visible" && setTick((n) => n + 1), 2000);
    return () => window.clearInterval(t);
  }, [live]);

  // Dropping the src closes the MJPEG connection when the card closes or switches mode.
  useEffect(() => {
    const img = imgRef.current;
    return () => {
      if (img) img.src = "";
    };
  }, [live]);

  const base = `/api/printers/${printerId}/webcam/${index}`;
  const src = live ? base : `${base}?snapshot=true&t=${tick}`;
  return (
    <figure className="webcam">
      <img ref={imgRef} src={src} alt={`${name} webcam`} />
      <figcaption className="row between">
        <span>
          <Icon name="camera" size={14} /> {name}
        </span>
        <button type="button" className="small secondary" onClick={() => setLive(!live)} aria-pressed={live}>
          {live ? "Stop live" : "Live view"}
        </button>
      </figcaption>
    </figure>
  );
}

/** Persistent printer status shown at the top of Library, Slice and Jobs. */
export function PrinterStrip() {
  const { printers } = useLivePrinters();
  const announce = useStateAnnouncements(printers);
  if (!printers || printers.length === 0) return null;
  return (
    <section className="printer-strip" aria-label="Printers">
      <ul className="printer-list">
        {printers.map((p) => (
          <PrinterCard key={p.id} p={p} />
        ))}
      </ul>
      <div className="sr-only" aria-live="polite">
        {announce}
      </div>
    </section>
  );
}

function useStateAnnouncements(printers: LivePrinter[] | null): string {
  const prev = useRef<Record<string, string>>({});
  const [message, setMessage] = useState("");
  useEffect(() => {
    if (!printers) return;
    const changes = printers
      .filter((p) => prev.current[p.id] && prev.current[p.id] !== p.state)
      .map((p) => `${p.name} is now ${stateInfo(p.state).label.toLowerCase()}`);
    prev.current = Object.fromEntries(printers.map((p) => [p.id, p.state]));
    if (changes.length) setMessage(changes.join(". "));
  }, [printers]);
  return message;
}
