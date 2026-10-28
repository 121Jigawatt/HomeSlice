import { ChangeEvent, DragEvent, FormEvent, useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { api, formatBytes, ImportPreview, Model, uploadModel } from "../api";
import { Icon } from "../icons";
import { ModelThumb } from "../ModelThumb";
import { GettingStarted } from "../setup";
import { useToast } from "../toast";
import { OverflowMenu, useConfirm, useUndoableDelete } from "../ui";

const ACCEPT = [".stl", ".3mf", ".obj", ".step", ".stp", ".amf"];
type Upload = { key: string; name: string; pct: number; state: "waiting" | "uploading" | "done" | "error"; error?: string };
type Sort = "newest" | "oldest" | "name" | "size";
const SORTS: [Sort, string][] = [
  ["newest", "Newest first"],
  ["oldest", "Oldest first"],
  ["name", "Name (A–Z)"],
  ["size", "Largest first"],
];

function sortModels(models: Model[], sort: Sort): Model[] {
  const out = [...models];
  if (sort === "newest") out.sort((a, b) => b.created_at - a.created_at);
  if (sort === "oldest") out.sort((a, b) => a.created_at - b.created_at);
  if (sort === "name") out.sort((a, b) => a.filename.localeCompare(b.filename, undefined, { numeric: true, sensitivity: "base" }));
  if (sort === "size") out.sort((a, b) => b.size - a.size);
  return out;
}

export default function LibraryPage() {
  const [models, setModels] = useState<Model[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [uploads, setUploads] = useState<Upload[]>([]);
  const [dragging, setDragging] = useState(false);
  const [sort, setSort] = useState<Sort>(() => (localStorage.getItem("library.sort") as Sort) || "newest");
  const fileInput = useRef<HTMLInputElement>(null);
  const notify = useToast();

  const load = () => api<Model[]>("/api/library").then(setModels).catch((e) => setError(e.message));
  useEffect(() => {
    load();
  }, []);

  // Thumbnails render in the background; refresh while recent previewable uploads still lack one.
  const pendingThumbs = (models ?? []).some(
    (m) => !m.has_thumbnail && [".stl", ".3mf", ".obj"].includes(m.ext) && Date.now() / 1000 - m.created_at < 600,
  );
  useEffect(() => {
    if (!pendingThumbs) return;
    const t = window.setTimeout(load, 3000);
    return () => window.clearTimeout(t);
  }, [models, pendingThumbs]);

  const busy = uploads.some((u) => u.state === "waiting" || u.state === "uploading");

  async function uploadFiles(files: File[]) {
    setError(null);
    const ok = files.filter((f) => ACCEPT.some((ext) => f.name.toLowerCase().endsWith(ext)));
    const skipped = files.length - ok.length;
    if (skipped) notify(`Skipped ${skipped} file${skipped === 1 ? "" : "s"}: only STL, 3MF, OBJ, STEP and AMF can be added.`, { tone: "error" });
    if (!ok.length) return;
    const batch: Upload[] = ok.map((f, i) => ({ key: `${Date.now()}-${i}-${f.name}`, name: f.name, pct: 0, state: "waiting" }));
    setUploads((u) => [...u.filter((x) => x.state !== "done"), ...batch]);
    const update = (key: string, patch: Partial<Upload>) =>
      setUploads((u) => u.map((x) => (x.key === key ? { ...x, ...patch } : x)));
    let done = 0;
    for (let i = 0; i < ok.length; i++) {
      const { key } = batch[i];
      update(key, { state: "uploading" });
      try {
        await uploadModel(ok[i], (pct) => update(key, { pct }));
        update(key, { state: "done", pct: 100 });
        done++;
        load();
      } catch (err) {
        update(key, { state: "error", error: (err as Error).message });
      }
    }
    if (done) notify(`Added ${done} model${done === 1 ? "" : "s"} to the library.`, { tone: "success" });
    // Finished rows clear after a moment; failures stay until dismissed.
    window.setTimeout(() => setUploads((u) => u.filter((x) => x.state !== "done")), 4000);
  }

  function onFiles(e: ChangeEvent<HTMLInputElement>) {
    const files = Array.from(e.target.files ?? []);
    e.target.value = "";
    uploadFiles(files);
  }

  const dropHandlers = {
    onDragOver: (e: DragEvent) => {
      if (!e.dataTransfer.types.includes("Files")) return;
      e.preventDefault();
      setDragging(true);
    },
    onDragLeave: (e: DragEvent) => {
      if (!e.currentTarget.contains(e.relatedTarget as Node)) setDragging(false);
    },
    onDrop: (e: DragEvent) => {
      if (!e.dataTransfer.files.length) return;
      e.preventDefault();
      setDragging(false);
      uploadFiles(Array.from(e.dataTransfer.files));
    },
  };

  const confirm = useConfirm();
  const undoableDelete = useUndoableDelete();
  async function remove(m: Model) {
    const ok = await confirm({
      title: `Delete ${m.filename}?`,
      body: <p>The model file is removed from the library. Sliced jobs keep their G-code.</p>,
      confirmLabel: "Delete model",
      danger: true,
    });
    if (!ok) return;
    undoableDelete(
      m.filename,
      () => setModels((ms) => (ms ?? []).filter((x) => x.id !== m.id)),
      load,
      () => api(`/api/library/${m.id}`, { method: "DELETE" }).then(load),
    );
  }

  const sorted = sortModels(models ?? [], sort);

  return (
    <section className={`stack drop-target${dragging ? " dragging" : ""}`} {...dropHandlers}>
      <div className="row between">
        <h1>Library</h1>
        <button type="button" onClick={() => fileInput.current?.click()} disabled={busy}>
          <Icon name="upload" size={18} /> Upload
        </button>
        <input
          ref={fileInput}
          type="file"
          accept={ACCEPT.join(",")}
          multiple
          hidden
          onChange={onFiles}
          aria-label="Choose model files to upload"
        />
      </div>
      <GettingStarted modelCount={models ? models.length : null} />
      {dragging && (
        <div className="drop-overlay" aria-hidden="true">
          <Icon name="upload" size={32} />
          Drop to add to the library
        </div>
      )}
      {uploads.length > 0 && (
        <div className="card stack" aria-live="polite">
          <h2>Uploads</h2>
          <ul className="list">
            {uploads.map((u) => (
              <li key={u.key} className="stack upload-row">
                <div className="row between small">
                  <span className="title grow">{u.name}</span>
                  <span className={u.state === "error" ? "error" : "muted"}>
                    {u.state === "waiting" ? "Waiting" : u.state === "uploading" ? `${u.pct}%` : u.state === "done" ? "Added" : "Failed"}
                  </span>
                  {u.state === "error" && (
                    <button
                      type="button"
                      className="icon-button secondary"
                      aria-label={`Dismiss ${u.name}`}
                      onClick={() => setUploads((x) => x.filter((y) => y.key !== u.key))}
                    >
                      <Icon name="close" size={14} />
                    </button>
                  )}
                </div>
                {u.state === "error" ? (
                  <p className="error small">{u.error}</p>
                ) : (
                  <progress max={100} value={u.pct} aria-label={`${u.name} upload progress`} />
                )}
              </li>
            ))}
          </ul>
        </div>
      )}
      <ImportFromLink onImported={load} />
      {error && <p className="error">{error}</p>}
      {models && models.length === 0 && uploads.length === 0 && (
        <div className="card empty">
          <Icon name="cube" size={40} />
          <h2>Your library is empty</h2>
          <p className="muted">
            Add STL, 3MF, OBJ or STEP files to slice them for your printers. Drag files onto this page, choose them from
            your device, or paste a Printables or MakerWorld link above.
          </p>
          <button type="button" onClick={() => fileInput.current?.click()}>
            <Icon name="upload" size={18} /> Choose files
          </button>
        </div>
      )}
      {models && models.length > 1 && (
        <div className="row between">
          <span className="muted small">
            {models.length} models · {formatBytes(models.reduce((n, m) => n + m.size, 0))}
          </span>
          <label className="row sort-label">
            Sort
            <select
              value={sort}
              onChange={(e) => {
                setSort(e.target.value as Sort);
                localStorage.setItem("library.sort", e.target.value);
              }}
            >
              {SORTS.map(([v, l]) => (
                <option key={v} value={v}>
                  {l}
                </option>
              ))}
            </select>
          </label>
        </div>
      )}
      <ul className="list model-grid">
        {sorted.map((m) => (
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
              <OverflowMenu
                label={`More actions for ${m.filename}`}
                items={[
                  { label: "Download", icon: "download", href: `/api/library/${m.id}/file`, download: true },
                  { label: "Delete…", icon: "trash", danger: true, onSelect: () => remove(m) },
                ]}
              />
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
      <h2 id="import-title">Import from link</h2>
      <form className="row" onSubmit={lookup} aria-labelledby="import-title">
        <label className="grow">
          Printables or MakerWorld model page
          <input
            type="url"
            inputMode="url"
            placeholder="https://www.printables.com/model/…"
            value={url}
            onChange={(e) => setUrl(e.target.value)}
            required
          />
        </label>
        <button className="small import-go" disabled={busy !== null}>
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
