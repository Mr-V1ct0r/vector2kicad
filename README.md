# Vector2KiCad

Convert an **Adobe Illustrator `.ai` (or an SVG) panel directly to a KiCad
footprint** (`.kicad_mod`), with no Inkscape round-trip, no manual SVG export,
and no silently truncated output.

It's a thin front-end over [`svg2mod`](https://github.com/svg2mod/svg2mod): all
the geometry (curve flattening, hole bridging, the KiCad writer) is svg2mod's.
Vector2KiCad adds the pieces that were forcing the Illustrator→Inkscape detour
and breaking imports:

1. **Reads `.ai` directly.** A modern `.ai` is a PDF whose top-level Illustrator
   layers are PDF optional-content groups. Vector2KiCad uses PyMuPDF to pull out
   the layer-tagged filled paths and build an SVG in memory, with no
   Illustrator, no Inkscape, no export step.
2. **Reads Illustrator/Inkscape SVGs too.** svg2mod finds layers *only* by an
   `inkscape:label`; Illustrator puts the name in `id`/`data-name`. vector2kicad
   reads those, decodes `_xHH_` escapes, matches a KiCad layer, and injects the
   label svg2mod expects. (Idempotent, so Inkscape SVGs pass straight through.)
3. **Correct size, automatically.** Illustrator SVGs are in points (72 DPI),
   Inkscape in 96 DPI; getting this wrong scales the whole panel. vector2kicad
   auto-detects it (and `.ai` is read in mm, so it's always right).
4. **Robust output.** svg2mod can crash mid-write on some panels (a hole it
   can't bridge → `IndexError`), leaving a file truncated by its final `)`,
   which is the KiCad *"Expecting … in 'clipboard', line N"* error. Vector2KiCad patches
   that crash to skip the unbridgeable hole (left filled, and reported) instead
   of aborting, and validates the output's paren balance so it always imports.

## Download

**Windows:** download the latest `Vector2KiCad.exe` from the
[Releases page](https://github.com/Mr-V1ct0r/vector2kicad/releases/latest) and
double-click it. No Python needed. The first run shows a Windows SmartScreen
notice because the app is not code signed; click More info, then Run anyway.

**Mac and Linux:** run from source (see below).

You also need [KiCad](https://www.kicad.org/) installed separately.

## Install (run from source)

```
pip install svg2mod lxml pymupdf
```
(Needs Python 3 with Tk. `pymupdf` is only needed for `.ai` input. Runs on
Windows, macOS, and Linux.)

## Use

**GUI** (double-click the **Vector2KiCad** desktop shortcut, or):
```
python vector2kicad_gui.py
```
Pick a `.ai` or `.svg`, check the layer mapping table, hit **CONVERT**.

The GUI remembers your options (precision, DPI, center, name, value, keep-SVG)
and your last-used folders between runs, saved per-user at
`%APPDATA%\Vector2KiCad\settings.json` (so it stays out of this folder and each
person who uses the tool keeps their own). Hover any option for a one-line
explanation, or press **Help** for the full rundown.

**CLI:**
```
python vector2kicad.py IN.ai  [-o OUT.kicad_mod] [-p 1.0]
python vector2kicad.py IN.svg [-o OUT.kicad_mod] [-p 1.0] [-d DPI]
python vector2kicad.py IN.ai  --list-layers      # just preview the mapping
```

## The one workflow rule

**Name your Illustrator layers after KiCad layers** such as `Edge.Cuts`, `F.SilkS`,
`F.Mask`, `F.Cu`, `B.Cu`, `B.SilkS`, `F.Fab`, `Dwgs.User`, etc. Any layer whose
name isn't a KiCad layer is ignored. Shapes must be **filled** (svg2mod converts
fills, not strokes), and for `.ai` input any **text must be outlined**
(Type ▸ Create Outlines): live text is skipped (you'll get a warning).

### Every supported layer

Edge.Cuts; `F.Cu` `B.Cu`; `F.SilkS` `B.SilkS`; `F.Mask` `B.Mask`;
`F.Paste` `B.Paste`; `F.Adhes` `B.Adhes`; `F.Fab` `B.Fab`; `F.CrtYd` `B.CrtYd`;
`Dwgs.User` `Cmts.User` `Eco1.User` `Eco2.User`; `Drill.Cu` `Drill.Mech`; and any
name ending in `.Keepout` (for example `F.Keepout`).

Advanced features Vector2KiCad inherits from svg2mod:

- **Drills.** A circle on `Drill.Cu` becomes a plated thru-hole (its stroke sets
  the pad size, its fill sets the drill); a circle on `Drill.Mech` becomes a
  mechanical hole. Drills must be circles (round holes only, no oval or slotted
  holes), so draw them as circles. Note: the `.ai` reader currently converts
  circles to paths, so drill layers work best from an SVG with real `<circle>`
  elements for now.
- **Keepout zones.** A `*.Keepout` layer (KiCad 6 or newer) blocks placement; the
  wildcard is any mix of F, B, and I.
- **Copper pads.** Copper-layer shapes can be turned into real pads via layer
  name options (see svg2mod's documentation).

That's it. Drop the `.ai` on Vector2KiCad. (Exporting an SVG yourself still
works and is slightly faster, since it skips the PDF read.)

## Notes / limits

- **`.ai` must be saved PDF-compatible** (the Illustrator default: "Create PDF
  Compatible File"). That's what carries the layers Vector2KiCad reads.
- **Precision** = approximate flattened-segment length in SVG px; smaller =
  smoother curves and a bigger file. `1.0` is a good default.
- **DPI** is auto-detected for SVGs (Illustrator 72 / Inkscape 96) and unused
  for `.ai` (read in mm). Override with `-d` / the DPI box only if needed.
- A hole that can't be bridged is left *filled* rather than cut, and the run
  reports how many. If one matters, nudge the art (offset the shape a hair) and
  re-run.
- Holes rely on opposite winding between outer/inner contours. Illustrator
  compound paths normally do this; if a hole fills in unexpectedly, make the
  shape a compound path (Object ▸ Compound Path ▸ Make).

## Compatibility

**Inputs**

- **Adobe Illustrator `.ai`**, read directly with no Illustrator running. Works
  with any version that saves "Create PDF Compatible File" on (the default, and
  the norm since Illustrator 9). Tested with Illustrator CC 2019 (version 23).
  The KiCad named layers must be top level layers, text must be outlined, and
  shapes filled.
- **Illustrator SVG**, both "Save As SVG" and "Export As SVG". The layer name is
  read from the group `id` or `data-name` (Illustrator `_xHH_` escapes are
  decoded). Coordinates are treated as 72 DPI automatically.
- **Inkscape SVG**, read through `inkscape:label` at 96 DPI.
- **Any other SVG** whose top level layer groups carry an `id`, `data-name`, or
  `inkscape:label` that matches a KiCad layer name. Unknown generators are
  assumed to be 96 DPI (override with the DPI option).

So it is not Inkscape only. Reading Illustrator directly is the point of the
tool.

**Output**

A KiCad footprint (`.kicad_mod`) written by svg2mod. It imports into modern
KiCad (version 6 and later).

**Runtime**

- Python 3 (tested on 3.13) with Tk. Dependencies: svg2mod 1.2, lxml, and
  pymupdf (pymupdf is only needed for `.ai` input).
- Runs on Windows, macOS, and Linux. Developed and tested on Windows 11. On
  Windows the window and taskbar icon uses `vector2kicad.ico`; on macOS and Linux
  the `.png` is used (on macOS the Dock icon is controlled by the app bundle, so
  it may not change). The "Open output folder" button uses the native file
  manager on each platform. The desktop shortcut and `.pyw` launcher are Windows
  conveniences; on macOS and Linux run `python vector2kicad_gui.py`.

## License / sharing

Copyright (C) 2026 00 Modular. Vector2KiCad is free software under the GNU
General Public License, version 2 or later. See the `LICENSE` file for the full
text. There is no warranty.

Vector2KiCad is a front-end that imports and extends
[`svg2mod`](https://github.com/svg2mod/svg2mod), which is licensed
**GPL v2 or later**. Because Vector2KiCad combines with svg2mod at runtime, the
combined work is covered by the GPL, so Vector2KiCad is distributed under
**GPL-2.0-or-later** as well. You are free to use, modify, and share it; if you
distribute it (or a modified version), keep it under GPLv2+, include this
license and svg2mod's copyright/license, keep the source available, and note
your changes. Vector2KiCad does not ship a modified copy of svg2mod. It
installs the stock `pip` package and adjusts its behavior at runtime (see the
`_patch_svg2mod` notes in `vector2kicad.py`). Not legal advice.

## Files

| File | Role |
|------|------|
| `vector2kicad.py` | engine + CLI (SVG normalizer, `.ai` PDF reader, svg2mod driver) |
| `vector2kicad_gui.py` | Tkinter GUI |
| `vector2kicad_launch.pyw` | double-click launcher (no console) |
