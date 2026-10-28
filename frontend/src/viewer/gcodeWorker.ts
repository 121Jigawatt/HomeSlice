/// <reference lib="webworker" />
// Parses G-code into extrusion/travel line segments grouped by layer.

interface Out {
  ext: number[];
  // 1 per extrusion segment that is support material (Orca/Prusa ";TYPE:Support...").
  extSupport: number[];
  trav: number[];
  extEnds: number[];
  travEnds: number[];
  layerZ: number[];
}

const ARC_SEGMENT_MM = 1;

function parse(text: string): Out {
  const out: Out = { ext: [], extSupport: [], trav: [], extEnds: [], travEnds: [], layerZ: [] };
  let support = false;
  let x = 0, y = 0, z = 0, e = 0;
  let absXYZ = true, absE = true;
  // Orca/Bambu/Prusa slicers mark layers with ";Z:<height>"; otherwise fall back to Z changes.
  const hasMarkers = text.includes("\n;Z:");
  let started = false;
  let layerZ = 0;

  const startLayer = (zValue: number) => {
    if (started) {
      out.extEnds.push(out.ext.length / 6);
      out.travEnds.push(out.trav.length / 6);
      out.layerZ.push(layerZ);
    }
    started = true;
    layerZ = zValue;
  };

  let pos = 0;
  const len = text.length;
  while (pos < len) {
    let end = text.indexOf("\n", pos);
    if (end < 0) end = len;
    const raw = text.slice(pos, end);
    pos = end + 1;

    if (raw.startsWith(";")) {
      if (hasMarkers && raw.startsWith(";Z:")) startLayer(parseFloat(raw.slice(3)));
      else if (raw.startsWith(";TYPE:")) support = /^;TYPE:\s*Support/i.test(raw);
      continue;
    }
    const semi = raw.indexOf(";");
    const line = (semi >= 0 ? raw.slice(0, semi) : raw).trim().toUpperCase();
    if (!line) continue;
    const words = line.split(/\s+/);
    const cmd = words[0];
    if (cmd === "G90") absXYZ = true, absE = true;
    else if (cmd === "G91") absXYZ = false, absE = false;
    else if (cmd === "M82") absE = true;
    else if (cmd === "M83") absE = false;
    else if (cmd === "G92") {
      for (const w of words.slice(1)) if (w[0] === "E") e = parseFloat(w.slice(1)) || 0;
    } else if (cmd === "G0" || cmd === "G1" || cmd === "G2" || cmd === "G3") {
      let nx = x, ny = y, nz = z, de = 0, i = 0, j = 0, hasE = false;
      for (const w of words.slice(1)) {
        const v = parseFloat(w.slice(1));
        if (Number.isNaN(v)) continue;
        switch (w[0]) {
          case "X": nx = absXYZ ? v : x + v; break;
          case "Y": ny = absXYZ ? v : y + v; break;
          case "Z": nz = absXYZ ? v : z + v; break;
          case "E": hasE = true; de = absE ? v - e : v; e = absE ? v : e + v; break;
          case "I": i = v; break;
          case "J": j = v; break;
        }
      }
      const extruding = hasE && de > 0 && (nx !== x || ny !== y);
      if (!hasMarkers && extruding && (!started || Math.abs(nz - layerZ) > 1e-4)) startLayer(nz);
      const target = extruding ? out.ext : out.trav;
      if (cmd === "G2" || cmd === "G3") {
        const cx = x + i, cy = y + j;
        const r = Math.hypot(i, j);
        let a0 = Math.atan2(y - cy, x - cx);
        let a1 = Math.atan2(ny - cy, nx - cx);
        if (cmd === "G2" && a1 >= a0) a1 -= 2 * Math.PI;
        if (cmd === "G3" && a1 <= a0) a1 += 2 * Math.PI;
        const steps = Math.max(2, Math.ceil((Math.abs(a1 - a0) * r) / ARC_SEGMENT_MM));
        let px = x, py = y, pz = z;
        for (let s = 1; s <= steps; s++) {
          const t = s / steps;
          const a = a0 + (a1 - a0) * t;
          const qx = cx + r * Math.cos(a), qy = cy + r * Math.sin(a), qz = z + (nz - z) * t;
          target.push(px, py, pz, qx, qy, qz);
          if (extruding) out.extSupport.push(support ? 1 : 0);
          px = qx; py = qy; pz = qz;
        }
      } else if (nx !== x || ny !== y || nz !== z) {
        target.push(x, y, z, nx, ny, nz);
        if (extruding) out.extSupport.push(support ? 1 : 0);
      }
      x = nx; y = ny; z = nz;
    }
  }
  if (started) startLayer(layerZ);
  return out;
}

self.onmessage = (event: MessageEvent<string>) => {
  try {
    const r = parse(event.data);
    const ext = new Float32Array(r.ext);
    const extSupport = Uint8Array.from(r.extSupport);
    const trav = new Float32Array(r.trav);
    const msg = {
      ext,
      extSupport,
      trav,
      extEnds: Int32Array.from(r.extEnds),
      travEnds: Int32Array.from(r.travEnds),
      layerZ: Float32Array.from(r.layerZ),
    };
    (self as unknown as Worker).postMessage(msg, [ext.buffer, extSupport.buffer, trav.buffer, msg.extEnds.buffer, msg.travEnds.buffer, msg.layerZ.buffer]);
  } catch (err) {
    (self as unknown as Worker).postMessage({ error: String(err) });
  }
};
