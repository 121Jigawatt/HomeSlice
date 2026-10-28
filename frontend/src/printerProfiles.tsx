import { FormEvent, useEffect, useState } from "react";
import { api, Preset, Printer } from "./api";
import { BED_TYPES, displayName } from "./presetNames";
import { useToast } from "./toast";

type Incompat = { process: Record<string, string>; filament: Record<string, string> };

/** Links a physical printer to the OrcaSlicer printer profile it prints, plus Slice-page defaults. */
export function PrinterProfiles({ printer, onSaved }: { printer: Printer; onSaved: (p: Printer) => void }) {
  const notify = useToast();
  const [presets, setPresets] = useState<Record<"printer" | "process" | "filament", Preset[]> | null>(null);
  const [preset, setPreset] = useState(printer.printer_preset ?? "");
  const [process, setProcess] = useState(printer.default_process ?? "");
  const [filaments, setFilaments] = useState<string[]>(printer.default_filaments.length ? printer.default_filaments : [""]);
  const [bedType, setBedType] = useState(printer.default_bed_type ?? "");
  const [coverage, setCoverage] = useState(String(printer.bed_coverage ?? 50));
  const [incompat, setIncompat] = useState<Incompat>({ process: {}, filament: {} });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    Promise.all(
      (["printer", "process", "filament"] as const).map((k) => api<Preset[]>(`/api/profiles/${k}?include_system=true`)),
    )
      .then(([printer, process, filament]) => setPresets({ printer, process, filament }))
      .catch((e) => setError(e.message));
  }, []);

  useEffect(() => {
    if (!preset) return setIncompat({ process: {}, filament: {} });
    api<Incompat>(`/api/profiles-compat?printer=${encodeURIComponent(preset)}`)
      .then(setIncompat)
      .catch(() => setIncompat({ process: {}, filament: {} }));
  }, [preset]);

  async function save(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const saved = await api<Printer>(`/api/printers/${printer.id}/profiles`, {
        method: "PUT",
        json: {
          printer_preset: preset || null,
          process: process || null,
          filaments: filaments.filter(Boolean),
          bed_type: bedType || null,
          bed_coverage: Math.min(100, Math.max(10, Math.round(Number(coverage) || 50))),
        },
      });
      notify(`Saved profiles for ${printer.name}.`, { tone: "success" });
      onSaved(saved);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  }

  const opts = (list: Preset[], bad: Record<string, string> = {}) =>
    list.map((p) => (
      <option key={`${p.source}:${p.name}`} value={p.name} disabled={!!bad[p.name]} title={p.name}>
        {displayName(p.name)}
        {p.source === "system" ? " (system)" : ""}
        {bad[p.name] ? ` \u2014 ${bad[p.name]}` : ""}
      </option>
    ));

  if (!presets) return <p className="muted small">{error ?? "Loading profiles…"}</p>;
  return (
    <form className="stack form-grid printer-profiles" onSubmit={save} aria-label={`Slicing profiles for ${printer.name}`}>
      <label className="span-2">
        OrcaSlicer printer profile
        <select
          value={preset}
          onChange={(e) => {
            setPreset(e.target.value);
            setProcess("");
            setFilaments([""]);
          }}
        >
          <option value="">Not set (can't receive prints)</option>
          {opts(presets.printer)}
        </select>
      </label>
      <label>
        Default process
        <select value={process} onChange={(e) => setProcess(e.target.value)} disabled={!preset}>
          <option value="">None</option>
          {opts(presets.process, incompat.process)}
        </select>
      </label>
      <label>
        Default plate
        <select value={bedType} onChange={(e) => setBedType(e.target.value)}>
          <option value="">None</option>
          {BED_TYPES.map(([v, l]) => (
            <option key={v} value={v}>
              {l}
            </option>
          ))}
        </select>
      </label>
      {filaments.map((f, i) => (
        <label key={i}>
          Default filament{filaments.length > 1 ? ` ${i + 1}` : ""}
          <select
            value={f}
            disabled={!preset}
            onChange={(e) => setFilaments(filaments.map((x, j) => (j === i ? e.target.value : x)))}
          >
            <option value="">None</option>
            {opts(presets.filament, incompat.filament)}
          </select>
        </label>
      ))}
      <div className="row span-2">
        <button type="button" className="small secondary" disabled={!preset} onClick={() => setFilaments([...filaments, ""])}>
          + Filament
        </button>
        {filaments.length > 1 && (
          <button type="button" className="small secondary" onClick={() => setFilaments(filaments.slice(0, -1))}>
            − Filament
          </button>
        )}
      </div>
      {error && <p className="error span-2">{error}</p>}
      <label className="span-2">
        Bed coverage before starting a new bed (%)
        <input
          type="number"
          min={10}
          max={100}
          step={5}
          value={coverage}
          onChange={(e) => setCoverage(e.target.value)}
          aria-describedby={`coverage-help-${printer.id}`}
        />
        <span id={`coverage-help-${printer.id}`} className="muted small">
          When several models are sliced together, they fill one bed until their footprints cover this much of it;
          the rest go on another bed, saved as a separate file. Default 50%.
        </span>
      </label>
      <button className="small span-2" disabled={busy}>
        {busy ? "Saving…" : "Save profiles"}
      </button>
    </form>
  );
}
