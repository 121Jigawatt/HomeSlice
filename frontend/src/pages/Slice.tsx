import { FormEvent, lazy, Suspense, useEffect, useMemo, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { api, Job, Model, Preset, Printer as PrinterConfig } from "../api";
import { ModelPicker } from "../ModelPicker";
import { BED_TYPES, bedLabel, displayName } from "../presetNames";
import { formatDuration, IDLE_STATES, PrinterBadge, useLivePrinters } from "../printers";
import { useToast } from "../toast";

type Kind = "printer" | "process" | "filament";
type Incompat = { process: Record<string, string>; filament: Record<string, string> };

const ModelViewer = lazy(() => import("../viewer/ModelViewer"));

type Supports = "" | "off" | "normal" | "tree";
const SUPPORT_LABELS: Record<Supports, string> = { "": "Process default", off: "Off", normal: "Normal", tree: "Tree" };
interface LayerInfo {
  nozzle: number;
  min: number;
  max: number;
  heights: number[];
  process_default: number | null;
}

function supportsFrom(ov: Record<string, string>): Supports {
  const on = ov.enable_support;
  if (!on) return "";
  if (on === "0" || on === "false") return "off";
  return ov.support_type?.startsWith("tree") ? "tree" : "normal";
}

/** Settings remembered before Off/Normal/Tree existed stored "true"/"false". */
function normalizeSupports(v: string): Supports {
  if (v === "false") return "off";
  if (v === "true") return "normal";
  return (["", "off", "normal", "tree"] as string[]).includes(v) ? (v as Supports) : "";
}

/** Everything needed to repeat a slice; remembered per model and restored from past jobs. */
interface SliceSettings {
  printer: string;
  process: string;
  filaments: string[];
  bedType: string;
  layerHeight: string;
  infill: string;
  supports: Supports;
  arrange: boolean;
  orient: boolean;
}

function fromJob(job: Job): SliceSettings {
  const p = job.params;
  const ov = p.overrides ?? {};
  return {
    printer: p.printer,
    process: p.process,
    filaments: p.filaments.length ? p.filaments : [""],
    bedType: ov.curr_bed_type ?? "Textured PEI Plate",
    layerHeight: ov.layer_height ?? "",
    infill: ov.sparse_infill_density?.replace("%", "") ?? "",
    supports: supportsFrom(ov),
    arrange: p.arrange ?? true,
    orient: p.orient ?? false,
  };
}

export default function SlicePage() {
  const [params] = useSearchParams();
  const navigate = useNavigate();
  const notify = useToast();
  const [models, setModels] = useState<Model[]>([]);
  const [modelsLoaded, setModelsLoaded] = useState(false);
  const [selected, setSelected] = useState<Set<string>>(() => new Set(params.getAll("model")));
  const [copies, setCopies] = useState<Record<string, number>>({});
  const countOf = (id: string) => copies[id] ?? 1;
  const [plates, setPlates] = useState<Record<string, number>>({});
  // Multi-plate 3MF projects default to their first plate rather than everything at once.
  const plateOf = (m: Model) => plates[m.id] ?? (m.plates && m.plates.length > 1 ? m.plates[0].index : 0);
  const [includeSystem, setIncludeSystem] = useState(false);
  const [presets, setPresets] = useState<Record<Kind, Preset[]>>({ printer: [], process: [], filament: [] });
  const [presetsLoaded, setPresetsLoaded] = useState(false);
  const [incompat, setIncompat] = useState<Incompat>({ process: {}, filament: {} });
  const [printer, setPrinter] = useState(() => localStorage.getItem("slice.printer") ?? "");
  const [process, setProcess] = useState(() => localStorage.getItem("slice.process") ?? "");
  const [filaments, setFilaments] = useState<string[]>(() =>
    JSON.parse(localStorage.getItem("slice.filaments") ?? '[""]'),
  );
  const [layerHeight, setLayerHeight] = useState("");
  const [bedType, setBedType] = useState(() => localStorage.getItem("slice.bedType") ?? "Textured PEI Plate");
  const [infill, setInfill] = useState("");
  const [supports, setSupports] = useState<Supports>("");
  const [layerInfo, setLayerInfo] = useState<LayerInfo | null>(null);
  const [arrange, setArrange] = useState(true);
  const [orient, setOrient] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [prefillNote, setPrefillNote] = useState<string | null>(null);
  const { printers: machines } = useLivePrinters();
  const [configs, setConfigs] = useState<PrinterConfig[] | null>(null);
  const [targetId, setTargetId] = useState("");
  const [prefilled, setPrefilled] = useState<boolean | null>(null);
  const target = configs?.find((c) => c.id === targetId && c.printer_preset);
  const machine = machines?.find((m) => m.id === target?.id);

  function applyTarget(c: PrinterConfig | undefined) {
    setTargetId(c?.id ?? "");
    localStorage.setItem("send.printer", c?.id ?? "");
    if (!c?.printer_preset) return;
    setPrinter(c.printer_preset);
    if (c.default_process) setProcess(c.default_process);
    if (c.default_filaments.length) setFilaments(c.default_filaments);
    if (c.default_bed_type) setBedType(c.default_bed_type);
  }

  useEffect(() => {
    api<PrinterConfig[]>("/api/printers").then(setConfigs).catch(() => setConfigs([]));
  }, []);

  // Once printers and any pre-fill are known: a pre-filled slice keeps its profile and targets the printer that
  // uses it; otherwise the last-used (or first) set-up printer's defaults apply.
  useEffect(() => {
    if (!configs || prefilled === null || targetId) return;
    const ready = configs.filter((c) => c.printer_preset);
    if (prefilled) {
      const match = ready.find((c) => c.printer_preset === printer);
      setTargetId(match?.id ?? "");
      return;
    }
    const stored = localStorage.getItem("send.printer");
    applyTarget(ready.find((c) => c.id === stored) ?? ready[0]);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [configs, prefilled]);

  function apply(s: SliceSettings) {
    setPrinter(s.printer);
    setProcess(s.process);
    setFilaments(s.filaments);
    setBedType(s.bedType);
    setLayerHeight(s.layerHeight);
    setInfill(s.infill);
    setSupports(normalizeSupports(s.supports));
    setArrange(s.arrange);
    setOrient(s.orient);
  }

  // Pre-fill from a past job (?from=) or from the settings last used for this model.
  useEffect(() => {
    const from = params.get("from");
    if (from) {
      api<Job>(`/api/jobs/${from}`)
        .then((job) => {
          apply(fromJob(job));
          setSelected(new Set(job.params.model_ids));
          setCopies(Object.fromEntries(job.params.model_ids.map((id, i) => [id, job.params.copies?.[i] ?? 1])));
          setPlates(Object.fromEntries(job.params.model_ids.map((id, i) => [id, job.params.model_plates?.[i] ?? 0])));
          setPrefillNote(`Settings copied from the job for ${job.title ?? "a previous slice"}.`);
          setPrefilled(true);
        })
        .catch((e) => {
          setError(e.message);
          setPrefilled(false);
        });
      return;
    }
    const ids = params.getAll("model");
    const saved = ids.length === 1 ? localStorage.getItem(`slice.model.${ids[0]}`) : null;
    if (saved) {
      try {
        apply(JSON.parse(saved) as SliceSettings);
        setPrefillNote("Using the settings you last sliced this model with.");
        setPrefilled(true);
        return;
      } catch {
        /* ignore a corrupt entry */
      }
    }
    setPrefilled(false);
  }, [params]);

  useEffect(() => {
    api<Model[]>("/api/library")
      .then(setModels)
      .catch((e) => setError(e.message))
      .finally(() => setModelsLoaded(true));
  }, []);

  useEffect(() => {
    const kinds: Kind[] = ["printer", "process", "filament"];
    Promise.all(kinds.map((k) => api<Preset[]>(`/api/profiles/${k}?include_system=${includeSystem}`)))
      .then(([printer, process, filament]) => {
        setPresets({ printer, process, filament });
        setPresetsLoaded(true);
      })
      .catch((e) => setError(e.message));
  }, [includeSystem]);

  useEffect(() => {
    if (!printer) {
      setIncompat({ process: {}, filament: {} });
      return;
    }
    let cancelled = false;
    api<Incompat>(`/api/profiles-compat?printer=${encodeURIComponent(printer)}`)
      .then((r) => !cancelled && setIncompat(r))
      .catch(() => !cancelled && setIncompat({ process: {}, filament: {} }));
    return () => {
      cancelled = true;
    };
  }, [printer]);

  useEffect(() => {
    if (!printer) return setLayerInfo(null);
    let cancelled = false;
    const q = new URLSearchParams({ printer });
    if (process) q.set("process", process);
    api<LayerInfo>(`/api/layer-heights?${q}`)
      .then((r) => !cancelled && setLayerInfo(r))
      .catch(() => !cancelled && setLayerInfo(null));
    return () => {
      cancelled = true;
    };
  }, [printer, process]);

  // A remembered or copied value outside the list is kept as an extra choice rather than silently dropped.
  const layerChoices = useMemo(() => {
    const list = (layerInfo?.heights ?? []).map((h) => String(h));
    if (layerHeight && !list.some((h) => Number(h) === Number(layerHeight))) list.push(layerHeight);
    return list.sort((a, b) => Number(a) - Number(b));
  }, [layerInfo, layerHeight]);

  const noUserPresets = useMemo(
    () => presetsLoaded && !includeSystem && presets.printer.length === 0,
    [presetsLoaded, includeSystem, presets.printer.length],
  );
  const selectedModels = useMemo(() => models.filter((m) => selected.has(m.id)), [models, selected]);
  const previewModels = useMemo(
    () =>
      selectedModels.flatMap((m) => Array.from({ length: copies[m.id] ?? 1 }, () => ({ ...m, plate: plateOf(m) }))),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [selectedModels, copies, plates],
  );
  const objectsIn = (m: Model) => {
    const p = plateOf(m);
    return p ? (m.plates?.find((x) => x.index === p)?.objects ?? 1) : (m.plates?.reduce((n, x) => n + x.objects, 0) || 1);
  };
  const totalObjects = selectedModels.reduce((n, m) => n + countOf(m.id) * objectsIn(m), 0);
  const chosenFilaments = filaments.filter(Boolean);

  const blockers: string[] = [];
  if (selected.size === 0) blockers.push("Select at least one model.");
  if (totalObjects > 200) blockers.push(`At most 200 objects can be sliced at once (you have ${totalObjects}).`);
  if (!printer) blockers.push("Choose a printer preset.");
  if (layerHeight && layerInfo && (Number(layerHeight) < layerInfo.min - 1e-9 || Number(layerHeight) > layerInfo.max + 1e-9))
    blockers.push(`Layer height ${layerHeight} mm is outside what this printer allows (${layerInfo.min}–${layerInfo.max} mm).`);
  if (target && printer !== target.printer_preset)
    blockers.push(`The printer profile must be ${target.name}'s ("${displayName(target.printer_preset ?? "")}").`);
  if (!process) blockers.push("Choose a process preset.");
  else if (incompat.process[process])
    blockers.push(`Process "${displayName(process)}" doesn't suit this printer (${incompat.process[process]}).`);
  if (chosenFilaments.length === 0) blockers.push("Choose a filament.");
  chosenFilaments
    .filter((f) => incompat.filament[f])
    .forEach((f) => blockers.push(`Filament "${displayName(f)}" doesn't suit this printer (${incompat.filament[f]}).`));

  function toggle(id: string) {
    const next = new Set(selected);
    next.has(id) ? next.delete(id) : next.add(id);
    setSelected(next);
  }

  async function submit(e: FormEvent) {
    e.preventDefault();
    if (blockers.length) return;
    setError(null);
    const overrides: Record<string, string> = { curr_bed_type: bedType };
    if (layerHeight) overrides.layer_height = layerHeight;
    if (infill) overrides.sparse_infill_density = `${infill}%`;
    if (supports === "off") overrides.enable_support = "0";
    if (supports === "normal" || supports === "tree") {
      overrides.enable_support = "1";
      // "auto": manual mode only supports painted areas, which HomeSlice can't paint.
      overrides.support_type = `${supports}(auto)`;
    }
    const settings: SliceSettings = { printer, process, filaments, bedType, layerHeight, infill, supports, arrange, orient };
    localStorage.setItem("slice.printer", printer);
    localStorage.setItem("slice.process", process);
    localStorage.setItem("slice.filaments", JSON.stringify(filaments));
    localStorage.setItem("slice.bedType", bedType);
    selected.forEach((id) => localStorage.setItem(`slice.model.${id}`, JSON.stringify(settings)));
    setBusy(true);
    try {
      const job = await api<{ id: string }>("/api/jobs", {
        method: "POST",
        json: {
          model_ids: [...selected],
          copies: [...selected].map(countOf),
          model_plates: [...selected].map((id) => {
            const m = models.find((x) => x.id === id);
            return m ? plateOf(m) : 0;
          }),
          printer,
          process,
          filaments: chosenFilaments,
          overrides,
          arrange,
          orient,
          target_printer_id: target?.id ?? null,
        },
      });
      notify("Slice started.", { action: { label: "View", to: `/jobs/${job.id}` } });
      navigate(`/jobs/${job.id}`);
    } catch (err) {
      setError((err as Error).message);
      notify(`Couldn't start slicing: ${(err as Error).message}`, { tone: "error" });
    } finally {
      setBusy(false);
    }
  }

  const options = (kind: Kind) => {
    const bad: Record<string, string> = kind === "printer" ? {} : incompat[kind];
    return [...presets[kind]]
      .sort((a, b) => Number(!!bad[a.name]) - Number(!!bad[b.name]))
      .map((p) => (
        <option
          key={`${p.source}:${p.name}`}
          value={p.name}
          disabled={!!bad[p.name]}
          title={bad[p.name] ? `${p.name} \u2014 ${bad[p.name]}` : p.name}
        >
          {displayName(p.name)}
          {p.source === "system" ? " (system)" : ""}
          {bad[p.name] ? ` \u2014 ${bad[p.name]}` : ""}
        </option>
      ));
  };

  return (
    <form className="stack slice-form" onSubmit={submit}>
      <h1 className="span-all">Slice</h1>
      {prefillNote && <p className="muted small span-all">{prefillNote}</p>}

      <fieldset className="card stack slice-models">
        <legend>Models</legend>
        <ModelPicker
          models={models}
          loaded={modelsLoaded}
          selected={selected}
          onToggle={toggle}
          copies={copies}
          onCopies={(id, n) => setCopies((c) => ({ ...c, [id]: n }))}
          plates={plates}
          onPlate={(id, p) => setPlates((c) => ({ ...c, [id]: p }))}
        />
        {selected.size > 0 && (
          <Suspense fallback={<div className="viewer placeholder muted small">Loading 3D preview…</div>}>
            <ModelViewer models={previewModels} printerPreset={printer} />
          </Suspense>
        )}
      </fieldset>

      {configs && configs.length > 0 && (
        <fieldset className="card stack">
          <legend>Target printer</legend>
          <label>
            Printer to slice for
            <select value={target?.id ?? ""} onChange={(e) => applyTarget(configs.find((c) => c.id === e.target.value))}>
              <option value="">None (slice only, choose profiles freely)</option>
              {configs.map((c) => (
                <option key={c.id} value={c.id} disabled={!c.printer_preset}>
                  {c.name}
                  {c.printer_preset ? "" : " \u2014 set its profiles in Settings first"}
                </option>
              ))}
            </select>
          </label>
          {target ? (
            <p className="muted small">
              Uses {target.name}'s printer profile <b title={target.printer_preset ?? ""}>{displayName(target.printer_preset ?? "")}</b>,
              so the result can only be sent to printers set up with that profile.
            </p>
          ) : (
            <p className="muted small">
              Without a target, the result can only be sent to a printer whose profile matches the one you pick.
            </p>
          )}
          {machine && (
            <div className="row small" aria-live="polite">
              <PrinterBadge state={machine.state} />
              {machine.state === "printing" || machine.state === "paused" ? (
                <span className="muted">
                  Busy with {machine.filename} · {formatDuration(machine.remaining)} left. You can slice now and send
                  when it's free.
                </span>
              ) : machine.needs_attention ? (
                <span className="muted">Confirm the bed is clear before it prints. You can slice now.</span>
              ) : IDLE_STATES.has(machine.state) ? (
                <span className="muted">Free to print.</span>
              ) : (
                <span className="muted">{machine.error ?? machine.message ?? "Check the printer before sending."}</span>
              )}
            </div>
          )}
        </fieldset>
      )}

      <fieldset className="card stack">
        <legend>Profiles</legend>
        {!presetsLoaded && <p className="muted small">Loading presets…</p>}
        {noUserPresets && (
          <p className="muted small">
            No custom presets found. Create or import them in Full Orca, or show system presets.
          </p>
        )}
        <label className="row check">
          <input type="checkbox" checked={includeSystem} onChange={(e) => setIncludeSystem(e.target.checked)} />
          Show system presets enabled in Orca
        </label>
        <label>
          Printer
          {target ? (
            <select value={printer} disabled title={`Set by ${target.name}. Change it under Settings \u2192 Printers.`}>
              <option value={printer}>{displayName(printer)} (from {target.name})</option>
            </select>
          ) : (
            <select value={printer} onChange={(e) => setPrinter(e.target.value)} required title={printer}>
              <option value="">Select…</option>
              {options("printer")}
            </select>
          )}
        </label>
        <label>
          Process
          <select value={process} onChange={(e) => setProcess(e.target.value)} required title={process}>
            <option value="">Select…</option>
            {options("process")}
          </select>
        </label>
        {printer && Object.keys(incompat.process).length > 0 && (
          <p className="muted small">Presets made for other printers are greyed out.</p>
        )}
        {filaments.map((f, i) => (
          <label key={i}>
            Filament {filaments.length > 1 ? i + 1 : ""}
            <div className="row">
              <select
                className="grow"
                value={f}
                title={f}
                onChange={(e) => setFilaments(filaments.map((x, j) => (j === i ? e.target.value : x)))}
                required
              >
                <option value="">Select…</option>
                {options("filament")}
              </select>
              {filaments.length > 1 && (
                <button
                  type="button"
                  className="small secondary"
                  onClick={() => setFilaments(filaments.filter((_, j) => j !== i))}
                  aria-label={`Remove filament ${i + 1}`}
                >
                  ✕
                </button>
              )}
            </div>
          </label>
        ))}
        {filaments.length < 16 && (
          <button type="button" className="small secondary" onClick={() => setFilaments([...filaments, ""])}>
            + Add filament
          </button>
        )}
        <label>
          Plate type
          <select value={bedType} onChange={(e) => setBedType(e.target.value)}>
            {BED_TYPES.map(([value, label]) => (
              <option key={value} value={value}>
                {label}
              </option>
            ))}
          </select>
        </label>
      </fieldset>

      <fieldset className="card form-grid">
        <legend>Quick overrides (optional)</legend>
        <label>
          Layer height
          <select value={layerHeight} onChange={(e) => setLayerHeight(e.target.value)} disabled={!printer}>
            <option value="">
              Process default{layerInfo?.process_default ? ` (${layerInfo.process_default} mm)` : ""}
            </option>
            {layerChoices.map((h) => (
              <option key={h} value={h}>
                {h} mm{h === String(layerInfo?.process_default) ? " (default)" : ""}
              </option>
            ))}
          </select>
          {layerInfo && (
            <span className="muted small">
              {layerInfo.nozzle} mm nozzle: {layerInfo.min}–{layerInfo.max} mm
            </span>
          )}
        </label>
        <label>
          Infill density (%)
          <input type="number" min="0" max="100" value={infill} onChange={(e) => setInfill(e.target.value)} placeholder="Profile default" />
        </label>
        <label>
          Supports
          <select value={supports} onChange={(e) => setSupports(e.target.value as Supports)}>
            <option value="">Profile default</option>
            <option value="off">Off</option>
            <option value="normal">Normal</option>
            <option value="tree">Tree</option>
          </select>
        </label>
        <label className="row check">
          <input type="checkbox" checked={arrange} onChange={(e) => setArrange(e.target.checked)} /> Auto-arrange
        </label>
        <label className="row check">
          <input type="checkbox" checked={orient} onChange={(e) => setOrient(e.target.checked)} /> Auto-orient
        </label>
      </fieldset>

      {error && <p className="error">{error}</p>}

      <section className="card stack" aria-labelledby="recap-title">
        <strong id="recap-title">Summary</strong>
        <dl className="recap small">
          <dt>Models</dt>
          <dd>
            {selectedModels.map((m) => (countOf(m.id) > 1 ? `${m.filename} \u00d7${countOf(m.id)}` : m.filename)).join(", ") ||
              "\u2014"}
            {totalObjects > 1 && <span className="muted"> ({totalObjects} objects)</span>}
          </dd>
          <dt>Printer</dt>
          <dd title={printer}>{printer ? displayName(printer) : "—"}</dd>
          <dt>Process</dt>
          <dd title={process}>{process ? displayName(process) : "—"}</dd>
          <dt>Filament</dt>
          <dd>{chosenFilaments.map(displayName).join(", ") || "—"}</dd>
          <dt>Plate</dt>
          <dd>{bedLabel(bedType)}</dd>
          <dt>Layer height</dt>
          <dd>{layerHeight ? `${layerHeight} mm` : "Process default"}</dd>
          <dt>Infill</dt>
          <dd>{infill ? `${infill}%` : "Process default"}</dd>
          <dt>Supports</dt>
          <dd>{SUPPORT_LABELS[supports]}</dd>
        </dl>
        {blockers.length > 0 ? (
          <div id="slice-blockers" className="blockers" role="status">
            <strong className="small">Before you can slice:</strong>
            <ul className="small">
              {blockers.map((b) => (
                <li key={b}>{b}</li>
              ))}
            </ul>
          </div>
        ) : (
          <p id="slice-blockers" className="muted small">
            Ready to slice.
          </p>
        )}
        <button disabled={busy || blockers.length > 0} aria-describedby="slice-blockers">
          {busy ? "Submitting…" : "Slice"}
        </button>
      </section>
    </form>
  );
}
