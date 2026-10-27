import { FormEvent, useEffect, useMemo, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { api, Model, Preset } from "../api";

type Kind = "printer" | "process" | "filament";

// Orca's curr_bed_type values and the labels Orca shows for them.
const BED_TYPES: [string, string][] = [
  ["Textured PEI Plate", "Textured PEI Plate"],
  ["High Temp Plate", "Smooth High Temp Plate"],
  ["Engineering Plate", "Engineering Plate"],
  ["Cool Plate", "Smooth Cool Plate"],
  ["Textured Cool Plate", "Textured Cool Plate"],
  ["Supertack Plate", "Cool Plate (SuperTack)"],
];

export default function SlicePage() {
  const [params] = useSearchParams();
  const navigate = useNavigate();
  const [models, setModels] = useState<Model[]>([]);
  const [selected, setSelected] = useState<Set<string>>(() => new Set(params.getAll("model")));
  const [includeSystem, setIncludeSystem] = useState(false);
  const [presets, setPresets] = useState<Record<Kind, Preset[]>>({ printer: [], process: [], filament: [] });
  const [printer, setPrinter] = useState(() => localStorage.getItem("slice.printer") ?? "");
  const [process, setProcess] = useState(() => localStorage.getItem("slice.process") ?? "");
  const [filaments, setFilaments] = useState<string[]>(() =>
    JSON.parse(localStorage.getItem("slice.filaments") ?? '[""]'),
  );
  const [layerHeight, setLayerHeight] = useState("");
  const [bedType, setBedType] = useState(() => localStorage.getItem("slice.bedType") ?? "Textured PEI Plate");
  const [infill, setInfill] = useState("");
  const [supports, setSupports] = useState<"" | "true" | "false">("");
  const [arrange, setArrange] = useState(true);
  const [orient, setOrient] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    api<Model[]>("/api/library").then(setModels).catch((e) => setError(e.message));
  }, []);

  useEffect(() => {
    const kinds: Kind[] = ["printer", "process", "filament"];
    Promise.all(kinds.map((k) => api<Preset[]>(`/api/profiles/${k}?include_system=${includeSystem}`)))
      .then(([printer, process, filament]) => setPresets({ printer, process, filament }))
      .catch((e) => setError(e.message));
  }, [includeSystem]);

  const noUserPresets = useMemo(
    () => !includeSystem && presets.printer.length === 0,
    [includeSystem, presets.printer.length],
  );

  function toggle(id: string) {
    const next = new Set(selected);
    next.has(id) ? next.delete(id) : next.add(id);
    setSelected(next);
  }

  async function submit(e: FormEvent) {
    e.preventDefault();
    setError(null);
    const overrides: Record<string, string> = { curr_bed_type: bedType };
    if (layerHeight) overrides.layer_height = layerHeight;
    if (infill) overrides.sparse_infill_density = `${infill}%`;
    if (supports) overrides.enable_support = supports;
    const chosenFilaments = filaments.filter(Boolean);
    localStorage.setItem("slice.printer", printer);
    localStorage.setItem("slice.process", process);
    localStorage.setItem("slice.filaments", JSON.stringify(filaments));
    localStorage.setItem("slice.bedType", bedType);
    setBusy(true);
    try {
      const job = await api<{ id: string }>("/api/jobs", {
        method: "POST",
        json: { model_ids: [...selected], printer, process, filaments: chosenFilaments, overrides, arrange, orient },
      });
      navigate(`/jobs/${job.id}`);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  }

  const options = (kind: Kind) =>
    presets[kind].map((p) => (
      <option key={`${p.source}:${p.name}`} value={p.name}>
        {p.name}
        {p.source === "system" ? " (system)" : ""}
      </option>
    ));

  return (
    <form className="stack" onSubmit={submit}>
      <h2>Slice</h2>

      <fieldset className="card stack">
        <legend>Models</legend>
        {models.length === 0 && <p className="muted">Upload a model in the Library first.</p>}
        {models.map((m) => (
          <label key={m.id} className="row check">
            <input type="checkbox" checked={selected.has(m.id)} onChange={() => toggle(m.id)} />
            {m.filename}
          </label>
        ))}
      </fieldset>

      <fieldset className="card stack">
        <legend>Profiles</legend>
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
          <select value={printer} onChange={(e) => setPrinter(e.target.value)} required>
            <option value="">Select…</option>
            {options("printer")}
          </select>
        </label>
        <label>
          Process
          <select value={process} onChange={(e) => setProcess(e.target.value)} required>
            <option value="">Select…</option>
            {options("process")}
          </select>
        </label>
        {filaments.map((f, i) => (
          <label key={i}>
            Filament {filaments.length > 1 ? i + 1 : ""}
            <div className="row">
              <select
                className="grow"
                value={f}
                onChange={(e) => setFilaments(filaments.map((x, j) => (j === i ? e.target.value : x)))}
                required
              >
                <option value="">Select…</option>
                {options("filament")}
              </select>
              {filaments.length > 1 && (
                <button type="button" className="small secondary" onClick={() => setFilaments(filaments.filter((_, j) => j !== i))}>
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

      <fieldset className="card stack">
        <legend>Quick overrides (optional)</legend>
        <label>
          Layer height (mm)
          <input type="number" step="0.01" min="0.02" max="1.2" value={layerHeight} onChange={(e) => setLayerHeight(e.target.value)} placeholder="Profile default" />
        </label>
        <label>
          Infill density (%)
          <input type="number" min="0" max="100" value={infill} onChange={(e) => setInfill(e.target.value)} placeholder="Profile default" />
        </label>
        <label>
          Supports
          <select value={supports} onChange={(e) => setSupports(e.target.value as "" | "true" | "false")}>
            <option value="">Profile default</option>
            <option value="true">On</option>
            <option value="false">Off</option>
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
      <button disabled={busy || selected.size === 0}>{busy ? "Submitting…" : "Slice"}</button>
    </form>
  );
}
