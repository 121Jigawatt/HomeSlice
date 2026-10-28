/** Friendlier preset labels: drops OrcaSlicer's " - Copy" / " @System" artefacts. Raw name stays available on hover. */
export function displayName(raw: string): string {
  const cleaned = raw
    .replace(/\s+-\s+Copy(\s*\(\d+\))?$/i, "")
    .replace(/\s*@System\b/i, "")
    .trim();
  return cleaned || raw;
}

// Orca's curr_bed_type values and the labels Orca shows for them.
export const BED_TYPES: [string, string][] = [
  ["Textured PEI Plate", "Textured PEI Plate"],
  ["High Temp Plate", "Smooth High Temp Plate"],
  ["Engineering Plate", "Engineering Plate"],
  ["Cool Plate", "Smooth Cool Plate"],
  ["Textured Cool Plate", "Textured Cool Plate"],
  ["Supertack Plate", "Cool Plate (SuperTack)"],
];
export const bedLabel = (v: string) => BED_TYPES.find(([value]) => value === v)?.[1] ?? v;
