/** Friendlier preset labels: drops OrcaSlicer's " - Copy" / " @System" artefacts. Raw name stays available on hover. */
export function displayName(raw: string): string {
  const cleaned = raw
    .replace(/\s+-\s+Copy(\s*\(\d+\))?$/i, "")
    .replace(/\s*@System\b/i, "")
    .trim();
  return cleaned || raw;
}
