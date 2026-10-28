import { Model } from "./api";

/** One printable object from the library (a 3MF build item, or a whole STL/OBJ/STEP model). */
export interface ModelPart {
  item: number;
  plate: number;
  footprint: number;
}

export interface PlannedPart {
  key: string;
  model: Model;
  item: number;
  footprint: number;
}

export interface Bed {
  parts: PlannedPart[];
  /** A 3MF project plate kept as its own bed. */
  projectPlate?: number;
  coverage: number;
}

export interface PlanEntry {
  model: Model;
  copies: number;
  /** 3MF: >0 one saved plate, 0 every object together, -1 each saved plate on its own bed. */
  plate: number;
  /** 3MF plates to use when `plate` is 0 or -1; empty means all. */
  selection: number[];
  parts: ModelPart[];
}

export const SEPARATE_PLATES = -1;

/** Which saved plates of a 3MF to slice; `plates` empty means all of them. */
export interface PlateChoice {
  plates: number[];
  separate: boolean;
}

/** The chosen plates (all when none are listed), in plate order. */
export function chosenPlates(m: Model, choice: PlateChoice): number[] {
  const all = (m.plates ?? []).map((p) => p.index);
  return choice.plates.length ? all.filter((i) => choice.plates.includes(i)) : all;
}

/** The `model_plates` / `plate_selection` values the server expects for one model. */
export function plateRequest(m: Model, choice: PlateChoice | undefined): { plate: number; selection: number[] } {
  if (!choice || !m.plates || m.plates.length < 2) return { plate: 0, selection: [] };
  const chosen = chosenPlates(m, choice);
  if (chosen.length === 1) return { plate: chosen[0], selection: [] };
  return {
    plate: choice.separate ? SEPARATE_PLATES : 0,
    selection: chosen.length === m.plates.length ? [] : chosen,
  };
}

/** Polygon area (mm²) of a printer's printable outline. */
export function polygonArea(points: [number, number][]): number {
  let a = 0;
  points.forEach(([x1, y1], i) => {
    const [x2, y2] = points[(i + 1) % points.length];
    a += x1 * y2 - x2 * y1;
  });
  return Math.abs(a) / 2;
}

/**
 * Same grouping the server uses when slicing (orca/jobs.py `_plate_groups`): kept project plates first, then the
 * rest packed in order, starting a new bed once footprints would exceed `coverage`% of the bed.
 */
export function planBeds(entries: PlanEntry[], coverage: number, bedArea: number): Bed[] {
  const fixed: Bed[] = [];
  const loose: PlannedPart[] = [];
  const area = (parts: PlannedPart[]) => parts.reduce((n, p) => n + p.footprint, 0);
  entries.forEach((e, i) => {
    const toPart = (p: ModelPart, copy: number): PlannedPart => ({
      key: `${i}:${e.model.id}:${p.item}:${copy}`,
      model: e.model,
      item: p.item,
      footprint: p.footprint,
    });
    const is3mf = e.model.ext === ".3mf";
    const wanted = (plate: number) => !e.selection.length || e.selection.includes(plate);
    if (is3mf && e.plate === SEPARATE_PLATES) {
      for (const plate of (e.model.plates ?? []).filter((p) => wanted(p.index))) {
        const objs = e.parts.filter((p) => p.plate === plate.index);
        for (let c = 0; c < e.copies; c++) {
          const parts = objs.map((p) => toPart(p, c));
          fixed.push({ parts, projectPlate: plate.index, coverage: (area(parts) / bedArea) * 100 });
        }
      }
    } else {
      const objs = !is3mf
        ? e.parts
        : e.plate > 0
          ? e.parts.filter((p) => p.plate === e.plate)
          : e.parts.filter((p) => wanted(p.plate));
      for (let c = 0; c < e.copies; c++) objs.forEach((p) => loose.push(toPart(p, c)));
    }
  });
  const beds: Bed[] = [...fixed];
  if (loose.length <= 1) {
    if (loose.length) beds.push({ parts: loose, coverage: (area(loose) / bedArea) * 100 });
    return beds;
  }
  const limit = (bedArea * Math.min(100, Math.max(10, coverage))) / 100;
  let current: PlannedPart[] = [];
  let covered = 0;
  for (const p of loose) {
    if (current.length && covered + p.footprint > limit) {
      beds.push({ parts: current, coverage: (covered / bedArea) * 100 });
      current = [];
      covered = 0;
    }
    current.push(p);
    covered += p.footprint;
  }
  beds.push({ parts: current, coverage: (covered / bedArea) * 100 });
  return beds;
}
