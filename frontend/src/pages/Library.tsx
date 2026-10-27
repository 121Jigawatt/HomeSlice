import { ChangeEvent, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api, formatBytes, Model, uploadModel } from "../api";

export default function LibraryPage() {
  const [models, setModels] = useState<Model[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [uploading, setUploading] = useState<{ name: string; pct: number } | null>(null);

  const load = () => api<Model[]>("/api/library").then(setModels).catch((e) => setError(e.message));
  useEffect(() => {
    load();
  }, []);

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
      {uploading && (
        <div className="card">
          Uploading {uploading.name}… {uploading.pct}%
          <progress max={100} value={uploading.pct} />
        </div>
      )}
      {error && <p className="error">{error}</p>}
      {models.length === 0 && <p className="muted">No models yet. Upload an STL, 3MF, OBJ or STEP file.</p>}
      <ul className="list">
        {models.map((m) => (
          <li key={m.id} className="card row between">
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
