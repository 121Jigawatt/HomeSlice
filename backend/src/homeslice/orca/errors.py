"""Plain-language explanations for OrcaSlicer CLI failures."""

from typing import Any

# code -> (title, likely cause, suggested fix). Codes are OrcaSlicer's negative CLI return codes.
_EXPLAIN: dict[int, tuple[str, str, str]] = {
    -2: ("Invalid slicer parameters", "A setting override has a value the slicer rejected.", "Clear the quick overrides and slice again."),
    -3: ("Model file missing", "The model was deleted or moved while the job was queued.", "Re-upload the model and slice again."),
    -5: ("Preset couldn't be read", "One of the selected presets is damaged or from a newer OrcaSlicer.", "Re-save the preset in Full OrcaSlicer, or pick another one."),
    -6: ("Model couldn't be read", "The file is corrupt or uses features this OrcaSlicer version doesn't understand.", "Re-export the model (STL or 3MF) from its source and upload it again."),
    -14: ("Out of memory", "The model is too detailed for the memory available to the slicer.", "Simplify or decimate the model, or raise the container's memory limit."),
    -16: ("Printer not compatible with this 3MF", "The 3MF was set up for a different printer model.", "Open it in Full OrcaSlicer, switch the printer there, and save it again."),
    -17: ("Process doesn't match the printer", "The process preset is made for a different printer or nozzle.", "Pick a process preset made for this printer (incompatible ones are greyed out)."),
    -18: ("Invalid values in the 3MF", "The project contains settings outside the allowed range.", "Open the 3MF in Full OrcaSlicer, fix the flagged settings and save it."),
    -20: ("Bed too small", "The printer's bed is smaller than the one the project was laid out for.", "Enable auto-arrange or choose a printer with a larger bed."),
    -21: ("Couldn't arrange the models", "The selected models don't fit on the plate together.", "Slice fewer models at once or scale them down."),
    -22: ("Couldn't auto-orient", "Auto-orient failed on this geometry.", "Turn off auto-orient and slice again."),
    -24: ("Unsupported 3MF version", "The 3MF was saved by a newer or beta slicer.", "Re-save it in Full OrcaSlicer, or export an STL instead."),
    -50: ("Nothing to print on the plate", "No object is fully inside the printable area.", "Enable auto-arrange, or check the model's size against the bed in the preview."),
    -51: ("Invalid slicing settings", "The combination of presets produced settings the slicer can't use.", "Try the printer's default process preset, then reapply your changes."),
    -52: ("Object outside the bed", "Part of a model is over the edge of the heated bed.", "Enable auto-arrange, or scale the model down so it fits."),
    -58: ("Slicing took too long", "The model is very complex for the chosen layer height.", "Simplify the model or use a larger layer height."),
    -59: ("Too many triangles", "The mesh is too detailed for the slicer.", "Decimate the model (e.g. in Blender or Meshmixer) and upload it again."),
    -61: ("Filament doesn't suit the plate", "A filament has no bed temperature for the selected plate type.", "Choose a different plate type on the Slice page."),
    -62: ("Filament temperatures too different", "The filaments need very different nozzle temperatures to print together.", "Use filaments with similar temperature ranges."),
    -63: ("Objects collide (print by object)", "Objects are too close for sequential printing.", "Space them further apart or switch to layer-by-layer printing."),
    -64: ("Objects collide", "Objects overlap on the plate.", "Enable auto-arrange or slice fewer objects."),
    -65: ("Spiral vase settings conflict", "Some settings can't be used with spiral vase mode.", "Disable spiral vase or use a single-wall, no-infill process."),
    -100: ("Slicing failed", "The slicer hit an error while processing the model; often non-manifold or broken geometry.", "Repair the mesh (e.g. in Windows 3D Builder or Meshmixer), then slice again."),
    -101: ("Tool path conflict", "Generated paths collide, often with the prime/wipe tower.", "Move the wipe tower or disable it, then slice again."),
}


def signed_code(returncode: int) -> int:
    """Linux reports Orca's negative codes as unsigned bytes (e.g. -17 -> 239)."""
    return returncode - 256 if returncode > 127 else returncode


def explain(code: int | None, raw: str | None, kind: str = "orca") -> dict[str, Any]:
    if kind == "timeout":
        return {
            "title": "Slicing timed out",
            "cause": "The slicer didn't finish within the configured time limit.",
            "fix": "Simplify the model, use a larger layer height, or raise HOMESLICE_SLICE_TIMEOUT_S.",
            "code": None,
            "raw": raw,
        }
    if code is not None and code in _EXPLAIN:
        title, cause, fix = _EXPLAIN[code]
    elif kind == "setup":
        title, cause, fix = "Couldn't start slicing", raw or "A required file or preset is missing.", "Check the selected presets and models, then try again."
    else:
        title = "Slicing failed"
        cause = raw or "OrcaSlicer stopped with an error."
        fix = "Open Technical details for the log, or try the job in Full OrcaSlicer to see the full message."
    return {"title": title, "cause": cause, "fix": fix, "code": code, "raw": raw}
