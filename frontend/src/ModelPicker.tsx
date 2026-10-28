import { useEffect, useRef, useState } from "react";
import { formatBytes, Model } from "./api";
import { Icon } from "./icons";
import { ModelThumb } from "./ModelThumb";

/** How many copies of one model to put on the plate. */
function Quantity({ name, value, onChange }: { name: string; value: number; onChange: (n: number) => void }) {
  const set = (n: number) => onChange(Math.min(MAX_COPIES, Math.max(1, Math.round(n) || 1)));
  return (
    <div className="quantity" role="group" aria-label={`Copies of ${name}`}>
      <button type="button" className="icon-button secondary" onClick={() => set(value - 1)} disabled={value <= 1} aria-label={`One fewer ${name}`}>
        −
      </button>
      <input
        type="number"
        inputMode="numeric"
        min={1}
        max={MAX_COPIES}
        value={value}
        onChange={(e) => set(Number(e.target.value))}
        aria-label={`Number of copies of ${name}`}
      />
      <button type="button" className="icon-button secondary" onClick={() => set(value + 1)} disabled={value >= MAX_COPIES} aria-label={`One more ${name}`}>
        +
      </button>
    </div>
  );
}

const MAX_COPIES = 100;

/** Selected models plus an "Add models" dialog to search the library and pick from it. */
export function ModelPicker({
  models,
  loaded,
  selected,
  onToggle,
  copies,
  onCopies,
  plates,
  onPlate,
}: {
  models: Model[];
  loaded: boolean;
  selected: Set<string>;
  onToggle: (id: string) => void;
  copies: Record<string, number>;
  onCopies: (id: string, n: number) => void;
  plates: Record<string, number>;
  onPlate: (id: string, plate: number) => void;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");

  useEffect(() => {
    if (open && dialog.current && !dialog.current.open) dialog.current.showModal();
  }, [open]);

  const close = () => {
    dialog.current?.close();
    setOpen(false);
    setQuery("");
  };

  const chosen = models.filter((m) => selected.has(m.id));
  const terms = query.toLowerCase().split(/\s+/).filter(Boolean);
  const matches = [...models]
    .sort((a, b) => b.created_at - a.created_at)
    .filter((m) => terms.every((t) => m.filename.toLowerCase().includes(t)));

  return (
    <>
      {chosen.length > 0 ? (
        <ul className="list picked-models">
          {chosen.map((m) => (
            <li key={m.id} className="row picked-model">
              <ModelThumb id={m.id} hasThumbnail={m.has_thumbnail} size={40} />
              <div className="grow stack picked-info">
                <span className="title">{m.filename}</span>
                {m.plates && m.plates.length > 1 && (
                  <label className="plate-select">
                    <span className="sr-only">Plate of {m.filename}</span>
                    <select value={plates[m.id] ?? m.plates[0].index} onChange={(e) => onPlate(m.id, Number(e.target.value))}>
                      {m.plates.map((p) => (
                        <option key={p.index} value={p.index}>
                          Plate {p.index}
                          {p.name ? `: ${p.name}` : ""} ({p.objects} object{p.objects === 1 ? "" : "s"})
                        </option>
                      ))}
                      <option value={0}>All plates, arranged together</option>
                      <option value={-1}>Every plate, each on its own bed</option>
                    </select>
                  </label>
                )}
              </div>
              <Quantity name={m.filename} value={copies[m.id] ?? 1} onChange={(n) => onCopies(m.id, n)} />
              <button
                type="button"
                className="icon-button secondary"
                onClick={() => onToggle(m.id)}
                aria-label={`Remove ${m.filename}`}
              >
                <Icon name="close" size={16} />
              </button>
            </li>
          ))}
        </ul>
      ) : (
        <p className="muted">
          {!loaded
            ? "Loading library…"
            : models.length === 0
              ? "Upload a model in the Library first."
              : "No models chosen yet."}
        </p>
      )}
      {models.length > 0 && (
        <button type="button" className="secondary" onClick={() => setOpen(true)}>
          + Add models
        </button>
      )}

      <dialog
        ref={dialog}
        className="dialog picker-dialog"
        aria-labelledby="picker-title"
        onCancel={(e) => {
          e.preventDefault();
          close();
        }}
      >
        {open && (
          <div className="stack">
            <div className="row between">
              <h2 id="picker-title">Add models</h2>
              <span className="muted small" aria-live="polite">
                {selected.size} selected
              </span>
            </div>
            <label>
              Search the library
              <input
                type="search"
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                placeholder="e.g. hydra arm"
                autoFocus
              />
            </label>
            <ul className="list picker-list">
              {matches.map((m) => (
                <li key={m.id}>
                  <label className="row check picker-row">
                    <input type="checkbox" checked={selected.has(m.id)} onChange={() => onToggle(m.id)} />
                    <ModelThumb id={m.id} hasThumbnail={m.has_thumbnail} size={40} />
                    <span className="grow">
                      <span className="title block">{m.filename}</span>
                      <span className="muted small block">
                        {m.ext.slice(1).toUpperCase()} · {formatBytes(m.size)} ·{" "}
                        {new Date(m.created_at * 1000).toLocaleDateString()}
                      </span>
                    </span>
                  </label>
                </li>
              ))}
              {matches.length === 0 && <li className="muted">No models match “{query}”.</li>}
            </ul>
            <div className="row end">
              <button type="button" onClick={close}>
                Done
              </button>
            </div>
          </div>
        )}
      </dialog>
    </>
  );
}
