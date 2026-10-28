import { ChangeEvent, FormEvent, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api, formatBytes, ImportPreview, Model, uploadModel } from "../api";
import { ModelThumb } from "../ModelThumb";
import { PrinterStrip } from "../printers";

export default function LibraryPage() {
  const [models, setModels] = useState<Model[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [uploading, setUploading] = useState<{ name: string; pct: number } | null>(null);

  const load = () => api<Model[]>("/api/library").then(setModels).catch((e) => setError(e.message));
  useEffect(() => {
    load();
  }, []);

  // Thumbnails render in the background; refresh while recent previewable uploads still lack one.
  const pendingThumbs = models.some(
    (m) => !m.has_thumbnail && [".stl", ".3mf", ".obj"].includes(m.ext) && Date.now() / 1000 - m.created_at < 600,
  );
  useEffect(() => {
    if (!pendingThumbs) return;
    const t = window.setTimeout(load, 3000);
    return () => window.clearTimeout(t);
  }, [models, pendingThumbs]);

  async function onFiles(e: ChangeEvent<HTMLInputElement>) {
    const files = Array.from(e.target.files ?? []);
    e.target.value = "";
    setError(null);
    for (const file of files) {
      setUploading({ name: file.name, pct: 0 });
      try {
        await uploadModel(file, (pct) => setUploading({ name: file.name, pct }));
      } catch (err) {
        setError(`${file.name}: ${(err as Error).message}`);
      }
    }
    setUploading(null);
    load();
  }

  async function remove(m: Model) {
    if (!confirm(`Delete ${m.filename}?`)) return;
    await api(`/api/library/${m.id}`, { method: "DELETE" }).catch((e) => setError(e.message));
    load();
  }

  return (
    <section className="stack">
      <div className="row between">
        <h2>Library</h2>
        <label className="button">
          Upload
          <input type="file" accept=".stl,.3mf,.obj,.step,.stp,.amf" multiple hidden onChange={onFiles} />
        </label>
      </div>
      <PrinterStrip />
      {uploading && (
        <div className="card">
          Uploading {uploading.name}… {uploading.pct}%
          <progress max={100} value={uploading.pct} />
        </div>
      )}
      <ImportFromLink onImported={load} />
      {error && <p className="error">{error}</p>}
      {models.length === 0 && <p className="muted">No models yet. Upload an STL, 3MF, OBJ or STEP file.</p>}
      <ul className="list">
        {models.map((m) => (
          <li key={m.id} className="card row between">
            <ModelThumb id={m.id} hasThumbnail={m.has_thumbnail} size={64} />
            <div className="grow">
              <div className="title">{m.filename}</div>
              <div className="muted small">
                {m.ext.slice(1).toUpperCase()} · {formatBytes(m.size)} · {new Date(m.created_at * 1000).toLocaleString()}
              </div>
            </div>
            <div className="row">
              <Link className="button small" to={`/slice?model=${m.id}`}>
                Slice
              </Link>
              <a className="button small secondary" href={`/api/library/${m.id}/file`}>
                ⬇
              </a>
              <button className="small danger" onClick={() => remove(m)} aria-label="Delete">
                ✕
              </button>
            </div>
          </li>
        ))}
      </ul>
    </section>
  );
}

function ImportFromLink({ onImported }: { onImported: () => void }) {
  const [url, setUrl] = useState("");
  const [preview, setPreview] = useState<ImportPreview | null>(null);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [image, setImage] = useState(0);
  const [busy, setBusy] = useState<"lookup" | "import" | null>(null);
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);

  async function lookup(e: FormEvent) {
    e.preventDefault();
    setBusy("lookup");
    setMsg(null);
    setPreview(null);
    try {
      const p = await api<ImportPreview>("/api/import/preview", { method: "POST", json: { url } });
      setPreview(p);
      setImage(0);
      setSelected(new Set(p.files.filter((f) => f.importable).map((f) => f.id)));
    } catch (err) {
      setMsg({ ok: false, text: (err as Error).message });
    } finally {
      setBusy(null);
    }
  }

  async function importSelected() {
    if (!preview) return;
    setBusy("import");
    setMsg(null);
    try {
      const records = await api<Model[]>("/api/import", {
        method: "POST",
        json: { source: preview.source, model_id: preview.id, file_ids: [...selected] },
      });
      setMsg({ ok: true, text: `Added ${records.length} file${records.length === 1 ? "" : "s"} to the library.` });
      setPreview(null);
      setUrl("");
      onImported();
    } catch (err) {
      setMsg({ ok: false, text: (err as Error).message });
    } finally {
      setBusy(null);
    }
  }

  function toggle(id: string) {
    const next = new Set(selected);
    next.has(id) ? next.delete(id) : next.add(id);
    setSelected(next);
  }

  return (
    <div className="card stack">
      <strong>Import from link</strong>
      <form className="row" onSubmit={lookup}>
        <input
          className="grow"
          type="url"
          inputMode="url"
          placeholder="Paste a Printables or MakerWorld link"
          value={url}
          onChange={(e) => setUrl(e.target.value)}
          required
        />
        <button className="small" disabled={busy !== null}>
          {busy === "lookup" ? "Loading…" : "Look up"}
        </button>
      </form>
      {msg && <p className={msg.ok ? "muted" : "error"}>{msg.text}</p>}
      {preview && (
        <div className="stack">
          {preview.images.length > 0 && (
            <div className="stack">
              <img className="preview-main" src={preview.images[image]} alt={preview.title} referrerPolicy="no-referrer" />
              {preview.images.length > 1 && (
                <div className="row thumbs">
                  {preview.images.map((src, i) => (
                    <button
                      key={src}
                      type="button"
                      className={`thumb${i === image ? " active" : ""}`}
                      onClick={() => setImage(i)}
                      aria-label={`Picture ${i + 1}`}
                    >
                      <img src={src} alt="" referrerPolicy="no-referrer" loading="lazy" />
                    </button>
                  ))}
                </div>
              )}
            </div>
          )}
          <div>
            <div className="title">{preview.title}</div>
            <div className="muted small">
              {preview.source === "printables" ? "Printables" : "MakerWorld"}
              {preview.author && ` · by ${preview.author}`}
              {preview.license && ` · ${preview.license}`} ·{" "}
              <a href={preview.url} target="_blank" rel="noopener noreferrer">
                Open on site
              </a>
            </div>
          </div>
          {preview.note && <p className="muted small">{preview.note}</p>}
          <ul className="list">
            {preview.files.map((f) => (
              <li key={f.id}>
                <label className="row check file-row">
                  <input
                    type="checkbox"
                    checked={selected.has(f.id)}
                    disabled={!preview.downloadable || !f.importable}
                    onChange={() => toggle(f.id)}
                  />
                  {f.preview ? (
                    <img className="file-thumb" src={f.preview} alt="" referrerPolicy="no-referrer" loading="lazy" />
                  ) : (
                    <span className="file-thumb" />
                  )}
                  <span className="grow">
                    <span className="title">{f.name}</span>
                    <span className="muted small">
                      {" "}
                      {f.size != null && formatBytes(f.size)}
                      {!f.importable && " · not a printable model file"}
                    </span>
                  </span>
                </label>
              </li>
            ))}
          </ul>
          {preview.downloadable ? (
            <button disabled={busy !== null || selected.size === 0} onClick={importSelected}>
              {busy === "import" ? "Importing…" : `Add ${selected.size} file${selected.size === 1 ? "" : "s"} to library`}
            </button>
          ) : (
            <a className="button secondary" href={preview.url} target="_blank" rel="noopener noreferrer">
              Open on MakerWorld
            </a>
          )}
        </div>
      )}
    </div>
  );
}
