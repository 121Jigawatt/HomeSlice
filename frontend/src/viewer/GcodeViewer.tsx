import { useEffect, useRef, useState } from "react";
import * as THREE from "three";
import { api } from "../api";
import { ACCENT, buildPlate, createViewer, Viewer } from "./scene";

const MAX_GCODE_BYTES = 200 * 1024 ** 2;

interface Parsed {
  ext: Float32Array;
  extSupport: Uint8Array;
  trav: Float32Array;
  extEnds: Int32Array;
  travEnds: Int32Array;
  layerZ: Float32Array;
}

const SUPPORT_COLOUR = 0xf59e0b;

/** Extrusion lines coloured per segment: model in the accent colour, supports in amber. */
function extrusionLines(data: Float32Array, support: Uint8Array): THREE.LineSegments {
  const geo = new THREE.BufferGeometry();
  geo.setAttribute("position", new THREE.BufferAttribute(data, 3));
  const colours = new Float32Array(data.length);
  const model = new THREE.Color(ACCENT);
  const sup = new THREE.Color(SUPPORT_COLOUR);
  for (let s = 0; s < support.length; s++) {
    const c = support[s] ? sup : model;
    colours.set([c.r, c.g, c.b, c.r, c.g, c.b], s * 6);
  }
  geo.setAttribute("color", new THREE.BufferAttribute(colours, 3));
  return new THREE.LineSegments(geo, new THREE.LineBasicMaterial({ vertexColors: true, transparent: true, opacity: 0.85 }));
}

function lines(data: Float32Array, color: number, opacity = 1): THREE.LineSegments {
  const geo = new THREE.BufferGeometry();
  geo.setAttribute("position", new THREE.BufferAttribute(data, 3));
  return new THREE.LineSegments(
    geo,
    new THREE.LineBasicMaterial({ color, transparent: opacity < 1, opacity }),
  );
}

export default function GcodeViewer({ jobId, file, printerPreset }: { jobId: string; file: string; printerPreset: string }) {
  const hostRef = useRef<HTMLDivElement>(null);
  const viewerRef = useRef<Viewer | null>(null);
  const partsRef = useRef<{ ext: THREE.LineSegments; trav: THREE.LineSegments; current: THREE.LineSegments } | null>(null);
  const [parsed, setParsed] = useState<Parsed | null>(null);
  const [layer, setLayer] = useState(0);
  const [showTravel, setShowTravel] = useState(false);
  const [hasSupport, setHasSupport] = useState(false);
  const [status, setStatus] = useState("Loading G-code…");

  useEffect(() => {
    if (!hostRef.current) return;
    const viewer = createViewer(hostRef.current);
    viewerRef.current = viewer;
    return () => {
      viewer.dispose();
      viewerRef.current = null;
      partsRef.current = null;
    };
  }, []);

  useEffect(() => {
    let cancelled = false;
    const worker = new Worker(new URL("./gcodeWorker.ts", import.meta.url), { type: "module" });
    (async () => {
      const url = `/api/jobs/${jobId}/files/${encodeURIComponent(file)}`;
      const res = await fetch(url, { credentials: "same-origin" });
      if (!res.ok) throw new Error(`Could not load ${file}`);
      const size = Number(res.headers.get("content-length") ?? 0);
      if (size > MAX_GCODE_BYTES) throw new Error("This G-code is too large to preview in the browser.");
      const text = await res.text();
      if (cancelled) return;
      setStatus("Parsing…");
      const result = await new Promise<Parsed>((resolve, reject) => {
        worker.onmessage = (e) => (e.data.error ? reject(new Error(e.data.error)) : resolve(e.data));
        worker.onerror = (e) => reject(new Error(e.message));
        worker.postMessage(text);
      });
      if (cancelled) return;
      setParsed(result);
      setHasSupport(result.extSupport.some((v) => v === 1));
      setLayer(Math.max(0, result.layerZ.length - 1));
      setStatus(result.layerZ.length ? "" : "No layers found in this G-code.");
    })().catch((e) => !cancelled && setStatus((e as Error).message));
    return () => {
      cancelled = true;
      worker.terminate();
    };
  }, [jobId, file]);

  // Build geometry once per parse; the layer slider only changes draw ranges.
  useEffect(() => {
    const viewer = viewerRef.current;
    if (!viewer || !parsed) return;
    const root = new THREE.Group();
    const ext = extrusionLines(parsed.ext, parsed.extSupport);
    const trav = lines(parsed.trav, 0x8b9ab3, 0.35);
    const current = lines(new Float32Array(parsed.ext), 0xf0fdfa);
    root.add(ext, trav, current);
    partsRef.current = { ext, trav, current };
    viewer.scene.add(root);

    let plate: THREE.Group | null = null;
    api<{ area: [number, number][]; height: number }>(`/api/printer-bed?name=${encodeURIComponent(printerPreset)}`)
      .then((bed) => {
        plate = buildPlate(bed.area, bed.height);
        viewer.scene.add(plate);
        viewer.render();
      })
      .catch(() => undefined);

    ext.geometry.computeBoundingBox();
    const box = ext.geometry.boundingBox ?? new THREE.Box3(new THREE.Vector3(), new THREE.Vector3(100, 100, 10));
    box.min.z = 0;
    viewer.frame(box.clone().expandByScalar(Math.max(box.getSize(new THREE.Vector3()).length() * 0.1, 5)));
    return () => {
      viewer.scene.remove(root);
      if (plate) viewer.scene.remove(plate);
      [ext, trav, current].forEach((l) => l.geometry.dispose());
      partsRef.current = null;
    };
  }, [parsed, printerPreset]);

  useEffect(() => {
    const parts = partsRef.current;
    if (!parts || !parsed || !parsed.layerZ.length) return;
    const extEnd = parsed.extEnds[layer];
    const extStart = layer > 0 ? parsed.extEnds[layer - 1] : 0;
    parts.ext.geometry.setDrawRange(0, extStart * 2);
    parts.current.geometry.setDrawRange(extStart * 2, (extEnd - extStart) * 2);
    parts.trav.geometry.setDrawRange(0, parsed.travEnds[layer] * 2);
    parts.trav.visible = showTravel;
    viewerRef.current?.render();
  }, [layer, showTravel, parsed]);

  const count = parsed?.layerZ.length ?? 0;
  return (
    <div className="viewer-wrap stack">
      <div
        ref={hostRef}
        className="viewer"
        tabIndex={0}
        role="application"
        aria-roledescription="G-code preview"
        aria-label="G-code preview. Drag or use arrow keys to orbit, plus and minus to zoom, R to reset."
      />
      {status && (
        <p className="muted small" aria-live="polite">
          {status}
        </p>
      )}
      {count > 0 && (
        <>
          <label>
            Layer {layer + 1} of {count} · Z {parsed!.layerZ[layer].toFixed(2)} mm
            <input
              type="range"
              min={0}
              max={count - 1}
              value={layer}
              onChange={(e) => setLayer(Number(e.target.value))}
              aria-valuetext={`Layer ${layer + 1} of ${count}`}
            />
          </label>
          <div className="row small gcode-legend" aria-label="Colour key">
            <span>
              <i style={{ background: "#14b8a6" }} /> Model
            </span>
            {hasSupport && (
              <span>
                <i style={{ background: "#f59e0b" }} /> Supports
              </span>
            )}
            <span>
              <i style={{ background: "#f0fdfa" }} /> Current layer
            </span>
          </div>
          <div className="row between">
            <label className="row check small">
              <input type="checkbox" checked={showTravel} onChange={(e) => setShowTravel(e.target.checked)} /> Show
              travel moves
            </label>
            <button type="button" className="small secondary" onClick={() => viewerRef.current?.reset()}>
              Reset view
            </button>
          </div>
        </>
      )}
    </div>
  );
}
