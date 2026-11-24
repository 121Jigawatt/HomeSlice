import { useEffect, useRef, useState } from "react";
import * as THREE from "three";
import { STLLoader } from "three/addons/loaders/STLLoader.js";
import { OBJLoader } from "three/addons/loaders/OBJLoader.js";
import { api, Model } from "../api";
import { ACCENT, buildPlate, createViewer, plateBox, Viewer } from "./scene";

// Browsers struggle to parse meshes far beyond this; the slicer itself has no such limit.
const MAX_PREVIEW_BYTES = 120 * 1024 ** 2;
// Same spacing the server adds to each footprint when planning beds (ARRANGE_SPACING_MM).
const GAP = 8;

interface Bed {
  area: [number, number][];
  height: number;
}

/** A model to draw; for a 3MF, `item` is one of its objects (each is laid out separately, as the slicer arranges). */
export type PreviewModel = Model & { item?: number; color?: string };

async function loadModel(m: PreviewModel): Promise<THREE.Object3D> {
  // STEP (CAD) and 3MF objects come from the server as STL; three.js' 3MF loader can't follow Bambu Studio's
  // multi-file objects.
  const isStep = m.ext === ".step" || m.ext === ".stp";
  const viaServer = isStep || m.ext === ".3mf";
  const url = viaServer ? `/api/library/${m.id}/mesh?item=${m.item ?? 0}` : `/api/library/${m.id}/file`;
  const res = await fetch(url, { credentials: "same-origin" });
  if (!res.ok) {
    const detail = await res.json().then((j) => j.detail as string).catch(() => "");
    throw new Error(detail || `Could not load ${m.filename}`);
  }
  const data = await res.arrayBuffer();
  if (data.byteLength > MAX_PREVIEW_BYTES) throw new Error(`${m.filename}: preview mesh is too large.`);
  const material = new THREE.MeshStandardMaterial({ color: m.color ?? ACCENT, roughness: 0.6, metalness: 0.05 });
  if (m.ext === ".stl" || viaServer) {
    try {
      if (data.byteLength < 84) throw new Error("Incomplete STL header");
      const expected = 84 + new DataView(data).getUint32(80, true) * 50;
      const header = new TextDecoder().decode(data.slice(0, 9));
      if (expected > data.byteLength && !/^[\s\S]{0,4}solid/.test(header)) throw new Error("Incomplete STL triangles");
      const geo = new STLLoader().parse(data);
      if (!geo.getAttribute("position").count) {
        geo.dispose();
        throw new Error("Empty STL geometry");
      }
      geo.computeVertexNormals();
      return new THREE.Mesh(geo, material);
    } catch {
      material.dispose();
      throw new Error(`${m.filename}: preview data is not a complete STL mesh. Try reloading or re-uploading the model.`);
    }
  }
  if (m.ext === ".obj") {
    const group = new OBJLoader().parse(new TextDecoder().decode(data));
    group.traverse((o) => {
      if ((o as THREE.Mesh).isMesh) (o as THREE.Mesh).material = material;
    });
    return group;
  }
  throw new Error(`${m.ext.slice(1).toUpperCase()} files can't be previewed in the browser`);
}

/** Places models in rows on the plate, resting on Z=0, centred on the bed. */
function layout(objects: THREE.Object3D[], bed: Bed): THREE.Box3 {
  const bedBox = new THREE.Box3();
  bed.area.forEach(([x, y]) => bedBox.expandByPoint(new THREE.Vector3(x, y, 0)));
  const bedCenter = bedBox.getCenter(new THREE.Vector3());
  const bedWidth = bedBox.max.x - bedBox.min.x;
  const boxes = objects.map((o) => new THREE.Box3().setFromObject(o));
  // Shelf packing: start a new row when the next object would overflow the bed width.
  let x = 0;
  let y = 0;
  let rowDepth = 0;
  objects.forEach((o, i) => {
    const b = boxes[i];
    const w = b.max.x - b.min.x;
    const d = b.max.y - b.min.y;
    if (x > 0 && x + w > bedWidth) {
      x = 0;
      y += rowDepth + GAP;
      rowDepth = 0;
    }
    o.position.x += x - b.min.x;
    o.position.y += -y - b.max.y;
    o.position.z += -b.min.z;
    x += w + GAP;
    rowDepth = Math.max(rowDepth, d);
  });
  const all = new THREE.Box3();
  objects.forEach((o) => all.union(new THREE.Box3().setFromObject(o)));
  const shift = bedCenter.clone().sub(all.getCenter(new THREE.Vector3()));
  shift.z = 0;
  objects.forEach((o) => o.position.add(shift));
  all.translate(shift);
  return all;
}

export default function ModelViewer({ models, printerPreset }: { models: PreviewModel[]; printerPreset: string }) {
  const hostRef = useRef<HTMLDivElement>(null);
  const viewerRef = useRef<Viewer | null>(null);
  const [bed, setBed] = useState<Bed | null>(null);
  const [status, setStatus] = useState<string>("Loading preview…");
  const [fit, setFit] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    const fallback: Bed = { area: [[0, 0], [256, 0], [256, 256], [0, 256]], height: 250 };
    if (!printerPreset) {
      setBed(fallback);
      return;
    }
    api<Bed>(`/api/printer-bed?name=${encodeURIComponent(printerPreset)}`)
      .then((b) => !cancelled && setBed(b))
      .catch(() => !cancelled && setBed(fallback));
    return () => {
      cancelled = true;
    };
  }, [printerPreset]);

  useEffect(() => {
    if (!hostRef.current) return;
    let viewer: Viewer;
    try {
      viewer = createViewer(hostRef.current);
    } catch {
      setStatus("3D preview unavailable in this browser. You can still edit settings and slice.");
      return;
    }
    viewerRef.current = viewer;
    return () => {
      viewer.dispose();
      viewerRef.current = null;
    };
  }, []);

  useEffect(() => {
    const viewer = viewerRef.current;
    if (!viewer || !bed) return;
    let cancelled = false;
    const root = new THREE.Group();
    root.add(buildPlate(bed.area, bed.height));
    viewer.scene.add(root);
    setFit(null);

    const previewable = models.filter((m) => m.size <= MAX_PREVIEW_BYTES);
    const skipped = models.length - previewable.length;
    if (models.length === 0) {
      setStatus("Select a model to preview it on the build plate.");
      viewer.frame(plateBox(bed.area, bed.height));
    } else {
      setStatus("Loading preview…");
      // Copies share one download and geometry.
      const loads = new Map<string, Promise<THREE.Object3D>>();
      const load = (m: PreviewModel) => {
        const key = `${m.id}:${m.item ?? 0}:${m.color ?? ""}`;
        if (!loads.has(key)) loads.set(key, loadModel(m));
        return loads.get(key)!.then((o) => o.clone());
      };
      Promise.allSettled(previewable.map(load)).then((results) => {
        if (cancelled) return;
        const objects = results.flatMap((r) => (r.status === "fulfilled" ? [r.value] : []));
        const failed = results.filter((r) => r.status === "rejected").length + skipped;
        objects.forEach((o) => root.add(o));
        const box = objects.length ? layout(objects, bed) : plateBox(bed.area, bed.height);
        const size = box.getSize(new THREE.Vector3());
        const bedBox = new THREE.Box3();
        bed.area.forEach(([x, y]) => bedBox.expandByPoint(new THREE.Vector3(x, y, 0)));
        const bedSize = bedBox.getSize(new THREE.Vector3());
        const problems = [];
        if (size.x > bedSize.x + 0.01 || size.y > bedSize.y + 0.01) problems.push("wider than the bed");
        if (size.z > bed.height + 0.01) problems.push(`taller than the ${bed.height} mm build height`);
        setFit(
          objects.length
            ? problems.length
              ? `Doesn't fit as placed: ${problems.join(" and ")} (${size.x.toFixed(0)}\u00d7${size.y.toFixed(0)}\u00d7${size.z.toFixed(0)} mm).`
              : `Fits: ${size.x.toFixed(0)}\u00d7${size.y.toFixed(0)}\u00d7${size.z.toFixed(0)} mm on a ${bedSize.x.toFixed(0)}\u00d7${bedSize.y.toFixed(0)} mm bed.`
            : null,
        );
        const reason = results.find((r): r is PromiseRejectedResult => r.status === "rejected")?.reason as Error | undefined;
        setStatus(
          failed
            ? `${failed} model(s) couldn't be previewed${reason?.message ? `: ${reason.message}` : " (too large or unsupported format)"}.`
            : "",
        );
        const view = box.clone().union(plateBox(bed.area, Math.min(bed.height, size.z || 60)));
        viewer.frame(objects.length ? box.clone().expandByScalar(Math.max(size.length() * 0.15, 5)) : view);
      });
    }
    return () => {
      cancelled = true;
      viewer.scene.remove(root);
      root.traverse((o) => {
        const mesh = o as THREE.Mesh;
        mesh.geometry?.dispose?.();
        if (mesh.material instanceof THREE.Material) mesh.material.dispose();
      });
      viewer.render();
    };
  }, [models, bed]);

  return (
    <div className="viewer-wrap stack">
      <div
        ref={hostRef}
        className="viewer"
        tabIndex={0}
        role="application"
        aria-roledescription="3D preview"
        aria-label="Build plate preview. Drag or use arrow keys to orbit, scroll or plus and minus to zoom, R to reset."
      />
      <div className="row between small">
        <span className={fit?.startsWith("Doesn't") ? "error" : "muted"} aria-live="polite">
          {fit ?? status}
        </span>
        <button type="button" className="small secondary" onClick={() => viewerRef.current?.reset()}>
          Reset view
        </button>
      </div>
      {fit && status && <span className="muted small">{status}</span>}
      <span className="muted small">Positions are approximate; auto-arrange in the slicer places the final layout.</span>
    </div>
  );
}
