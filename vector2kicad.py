#!/usr/bin/env python3
r"""
vector2kicad -- convert an Adobe Illustrator (or any) SVG panel directly to a
KiCad footprint (.kicad_mod), with no Inkscape round-trip.

This EXTENDS svg2mod rather than reimplementing it: all the hard geometry
(curve flattening, winding/hole bridging, the KiCad writer) stays svg2mod's,
proven code. We add only the two pieces that were forcing the Inkscape detour
and causing today's broken import:

  1) Illustrator front-end (the normalizer).
     svg2mod finds layers ONLY by an `inkscape:label` attribute on each group
     (svg/svg.py reads nothing else -- its fallback to `id` is even dead code:
     `self.name == self.id` is a `==` typo, not an assignment). Illustrator
     writes the layer name into `id` / `data-name` instead, so svg2mod sees
     unnamed layers that map to nothing -- hence the Inkscape relabel step.
     normalize_svg() reads data-name/id, decodes Illustrator's _xHH_ escapes,
     matches the result to a real KiCad layer, and injects the inkscape:label
     svg2mod expects. It's idempotent, so it also passes Inkscape SVGs through
     unchanged.

  2) Robust writer.
     svg2mod can truncate its output by exactly the final ')' that closes the
     (module ...) -- the file then fails to import with KiCad's
     "Expecting ... in 'clipboard'" error two lines past EOF. validate_and_
     repair() checks the s-expression paren balance and repairs a missing
     closer, so the output always imports.

CLI:
    python vector2kicad.py IN.svg [-o OUT.kicad_mod] [--precision 1.0]
           [--name NAME] [--value VALUE] [--center] [--dpi 96] [--dump-svg P]

Library (used by the GUI):
    from vector2kicad import convert, scan_layers, LAYER_NAMES

Needs: svg2mod (pip install svg2mod) and lxml, both already present on this box.
"""

import argparse
import importlib.metadata
import logging
import os
import re
import sys
import tempfile

from lxml import etree

from svg2mod.exporter import Svg2ModExportLatest
from svg2mod.importer import Svg2ModImport
from svg2mod.coloredlogger import logger as svg2mod_logger
from svg2mod.svg2mod import PolygonSegment

# Vector2KiCad patches svg2mod's internals, so it is built against one exact
# svg2mod version. Warn if a different one is installed. This matters only for
# people running from source; the exe bundles the tested version. See
# requirements.txt.
EXPECTED_SVG2MOD = "1.2"
try:
    SVG2MOD_VERSION = importlib.metadata.version("svg2mod")
except Exception:
    SVG2MOD_VERSION = "unknown"


def svg2mod_version_note():
    """Return a warning string if the installed svg2mod is not the tested
    version, else None."""
    if SVG2MOD_VERSION != EXPECTED_SVG2MOD:
        return (f"svg2mod {SVG2MOD_VERSION} is installed, but Vector2KiCad was "
                f"built and tested against svg2mod {EXPECTED_SVG2MOD}. It may "
                f"misbehave. Install the tested version with:  "
                f"pip install svg2mod=={EXPECTED_SVG2MOD}")
    return None


# Marker prefix so convert() can count skipped holes from the captured log.
# The warning continues with per-hole detail (layer + mm location) so the user
# can find the offending shape; the count matches on this prefix.
_SKIP_MSG = "Could not bridge a hole into its outline; leaving it filled."

# Set by the _write_items wrapper so the insertion-point patch can name the
# KiCad layer the failing hole is on. Single conversion at a time, so a simple
# module global is enough (the GUI runs one convert on one worker thread).
_CURRENT_LAYER = None


def _patch_svg2mod():
    """Stop svg2mod from aborting a whole export on one unbridgeable hole, and
    make the one it skips identifiable.

    In svg2mod.svg2mod.PolygonSegment._find_insertion_point (line ~231),
        best = [self, intersections[self][0]]
    assumes the vertical ray cast up from a hole's lowest point always hits the
    outer outline. When it crosses the outline ZERO times -- a hole mis-paired
    to an outline that doesn't actually contain it, a ray grazing a vertex, or
    an open/self-intersecting contour -- intersections[self] is empty ->
    IndexError, and the entire footprint write is truncated: exactly the broken
    .kicad_mod that fails to import in KiCad.

    inline() already guards `if insertion is not None`, i.e. it is written to
    skip a hole whose insertion point can't be found. The method just never
    returns None; it crashes first. We wrap it to honor that contract: on the
    IndexError, warn (reporting the hole's layer, size and mm position so it can
    be located) and return None, so that one hole stays filled and the rest of
    the panel still converts.

    A second wrapper on Svg2ModExport._write_items records the layer currently
    being written, since _find_insertion_point has no reference back to the
    exporter. Both patched once, in-process only -- the installed svg2mod
    package is left untouched.
    """
    if getattr(PolygonSegment._find_insertion_point, "_vector2kicad_patched", False):
        return

    # Track the current layer for the diagnostic.
    from svg2mod.exporter import Svg2ModExport
    _orig_write_items = Svg2ModExport._write_items

    def _tracking_write_items(self, items, layer, flip=False):
        global _CURRENT_LAYER
        _CURRENT_LAYER = layer
        return _orig_write_items(self, items, layer, flip)

    _tracking_write_items._vector2kicad_patched = True
    Svg2ModExport._write_items = _tracking_write_items

    _orig = PolygonSegment._find_insertion_point

    def _safe_find_insertion_point(self, hole, holes, other_insertions):
        try:
            return _orig(self, hole, holes, other_insertions)
        except IndexError:
            svg2mod_logger.warning(_describe_unbridged_hole(self, hole))
            return None

    _safe_find_insertion_point._vector2kicad_patched = True
    PolygonSegment._find_insertion_point = _safe_find_insertion_point

    # Winding normalizer: svg2mod carves a hole only when its contour winds
    # OPPOSITE to the outline it sits in; it ignores the SVG/PDF even-odd fill
    # rule. Illustrator's "Make Compound Path" / Pathfinder "Minus Front"
    # routinely emit even-odd compound paths whose holes wind the SAME way as
    # the outer -- they look holed in Illustrator but svg2mod fills them solid
    # (e.g. a B.Cu pour whose windows never cut). inline() is called as
    # outline.inline(holes), i.e. svg2mod has already grouped the holes by
    # containment, so right here we can force each hole to the opposite winding
    # before the geometry is bridged. Holes already opposite are left alone, so
    # correctly-wound art (and Inkscape SVGs) is unaffected. Runs on every
    # input, .ai and .svg alike, before the back-layer x-flip's effect matters
    # (the flip reverses outline and holes together, preserving the fix).
    _orig_inline = PolygonSegment.inline

    def _winding_fixed_inline(self, segments):
        try:
            outer_cw = _signed_area(self.points) > 0
            for hole in segments:
                area = _signed_area(hole.points)
                if area == 0:
                    continue
                if (area > 0) == outer_cw:          # same winding as outline
                    hole._set_points(hole.points[::-1])
                    if getattr(hole, "calc_bbox", None):
                        hole.calc_bbox()
        except Exception:                            # never block the convert
            pass
        return _orig_inline(self, segments)

    _winding_fixed_inline._vector2kicad_patched = True
    PolygonSegment.inline = _winding_fixed_inline


def _signed_area(points):
    """Shoelace signed area of a point list (svg.Point). >0 and <0 are the two
    winding senses; ~0 is degenerate. Sign convention is only used to COMPARE an
    outline with its holes, so which sense is 'CW' doesn't matter here."""
    a = 0.0
    n = len(points)
    for i in range(n):
        p, q = points[i], points[(i + 1) % n]
        a += p.x * q.y - q.x * p.y
    return a / 2.0


def _describe_unbridged_hole(outline, hole):
    """Build the skip warning for one hole svg2mod couldn't bridge, naming the
    layer and the hole's size + position in mm (final KiCad footprint coords --
    points are already transformed by the time inlining runs). Starts with
    _SKIP_MSG so convert() can count these."""
    try:
        xs = [p.x for p in hole.points]
        ys = [p.y for p in hole.points]
        hx0, hx1, hy0, hy1 = min(xs), max(xs), min(ys), max(ys)
        w, h = hx1 - hx0, hy1 - hy0
        cx, cy = (hx0 + hx1) / 2, (hy0 + hy1) / 2
        (ox0, oy0), (ox1, oy1) = (outline.bbox[0].x, outline.bbox[0].y), \
                                 (outline.bbox[1].x, outline.bbox[1].y)
        layer = _CURRENT_LAYER or "?"
        return (f"{_SKIP_MSG} Layer {layer}: hole ~{w:.2f} x {h:.2f} mm centred "
                f"at ({cx:.2f}, {cy:.2f}) mm, inside a shape spanning "
                f"({ox0:.1f}, {oy0:.1f}) to ({ox1:.1f}, {oy1:.1f}) mm. "
                f"(Coords are KiCad footprint mm; +Y is down, as in the SVG.)")
    except Exception:
        return _SKIP_MSG


_patch_svg2mod()

# --------------------------------------------------------------------------- #
# Layer vocabulary -- taken straight from svg2mod so we never drift from what
# the exporter actually accepts. layer_map keys are the valid KiCad layer names
# (plus one regex entry, r'\S+\.Keepout', which we match separately).
# --------------------------------------------------------------------------- #
_RAW_MAP = Svg2ModExportLatest.layer_map
LAYER_NAMES = sorted(k for k in _RAW_MAP if "\\" not in k)      # plain names
_KEEPOUT_RE = re.compile(r"\S+\.Keepout$")                      # e.g. F.Keepout
_LOWER_TO_CANON = {n.lower(): n for n in LAYER_NAMES}

SVG_NS = "http://www.w3.org/2000/svg"
INKSCAPE_NS = "http://www.inkscape.org/namespaces/inkscape"

# DPI per SVG origin. svg2mod turns unit-less / px coordinates into mm with
# 25.4/dpi, so the DPI must match the editor that wrote the file or the whole
# panel comes out the wrong size. Illustrator's SVG plug-in writes px == points
# (72/inch); Inkscape uses 96/inch. (For files that carry real mm units the DPI
# cancels out, so 96 is harmless there.)
DPI_ILLUSTRATOR = 72
DPI_INKSCAPE = 96


def detect_dpi(svg_path):
    """Guess the correct DPI from who wrote the SVG. Returns (dpi, source_str).
    Illustrator -> 72, Inkscape -> 96, unknown -> 96 (safe for mm files)."""
    try:
        with open(svg_path, "r", encoding="utf-8", errors="replace") as fh:
            head = fh.read(4000)
    except OSError:
        return DPI_INKSCAPE, "default"
    low = head.lower()
    # Illustrator signatures: the generator comment, its enable-background and
    # its &ns_* entity refs all mark a point-based export.
    if ("adobe illustrator" in low or "enable-background:new" in low
            or "ns_extend" in low or "i:pgf" in low):
        return DPI_ILLUSTRATOR, "Illustrator"
    if "inkscape" in low:
        return DPI_INKSCAPE, "Inkscape"
    return DPI_INKSCAPE, "unknown (assumed 96)"
_G = f"{{{SVG_NS}}}g"
_LABEL = f"{{{INKSCAPE_NS}}}label"
_GROUPMODE = f"{{{INKSCAPE_NS}}}groupmode"

# Illustrator escapes characters that are illegal in an XML id as _xHH_ /
# _xHHHH_ (hex). "F_x2E_SilkS" -> "F.SilkS", "My_x20_Layer" -> "My Layer".
_ESCAPE_RE = re.compile(r"_x([0-9A-Fa-f]{2,6})_")


# --------------------------------------------------------------------------- #
# Layer-name resolution
# --------------------------------------------------------------------------- #
def _unescape(name):
    """Decode Illustrator _xHH_ hex escapes in an id."""
    if not name:
        return ""
    return _ESCAPE_RE.sub(lambda m: chr(int(m.group(1), 16)), name).strip()


def match_layer(data_name, elt_id):
    """Resolve a group's data-name/id to a canonical KiCad layer name, or None.

    Tries, in order of trust: the exact data-name (Illustrator "Export As"
    preserves the original name there), the hex-decoded id, and finally the id
    with '_' read as '.' (covers "F_SilkS" when a dot didn't survive). Matching
    is case-insensitive; the canonical KiCad casing is returned.
    """
    candidates = []
    if data_name:
        candidates.append(data_name.strip())
    dec = _unescape(elt_id)
    if dec:
        candidates.append(dec)
        if "_" in dec:
            candidates.append(dec.replace("_", "."))
    for cand in candidates:
        canon = _LOWER_TO_CANON.get(cand.lower())
        if canon:
            return canon
        if _KEEPOUT_RE.match(cand):          # keepout layers keep their name
            return cand
    return None


# --------------------------------------------------------------------------- #
# Scan / normalize
# --------------------------------------------------------------------------- #
class LayerRow:
    """One group's mapping result, for reporting to the user / GUI table."""

    __slots__ = ("name", "elt_id", "data_name", "layer", "already_labeled")

    def __init__(self, name, elt_id, data_name, layer, already_labeled):
        self.name = name                     # best human-readable name
        self.elt_id = elt_id
        self.data_name = data_name
        self.layer = layer                   # canonical KiCad layer, or None
        self.already_labeled = already_labeled

    @property
    def mapped(self):
        return self.layer is not None


def _group_rows(root):
    """Yield (element, LayerRow) for every group that looks like a layer:
    one that already carries an inkscape:label, or has an id / data-name."""
    for g in root.iter(_G):
        existing = g.get(_LABEL)
        elt_id = g.get("id", "") or ""
        data_name = g.get("data-name", "") or ""
        if not (existing or elt_id or data_name):
            continue
        if existing:                         # Inkscape file: trust its label
            layer = _LOWER_TO_CANON.get(existing.lower()) or (
                existing if _KEEPOUT_RE.match(existing) else None)
            row = LayerRow(existing, elt_id, data_name, layer, True)
        else:
            layer = match_layer(data_name, elt_id)
            name = data_name or _unescape(elt_id) or elt_id
            row = LayerRow(name, elt_id, data_name, layer, False)
        yield g, row


def scan_layers(src_path):
    """Return a list of LayerRow for src_path WITHOUT converting -- feeds the
    GUI's layer table and a CLI dry preview. Handles .ai (read via PyMuPDF)."""
    if src_path.lower().endswith(".ai"):
        fd, tmp = tempfile.mkstemp(suffix=".svg", prefix="vector2kicad_scan_")
        os.close(fd)
        try:
            ai_to_svg(src_path, tmp)
            return [row for _, row in _group_rows(etree.parse(tmp).getroot())]
        finally:
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass
    tree = etree.parse(src_path)
    return [row for _, row in _group_rows(tree.getroot())]


def normalize_svg(src_path, dst_path):
    """Write a svg2mod-ready copy of src_path to dst_path: every group that
    resolves to a KiCad layer gets the inkscape:label (and groupmode) svg2mod
    reads. Groups that already have a label are left untouched (idempotent, so
    existing Inkscape SVGs pass straight through). Returns the list of
    LayerRow describing what was mapped."""
    tree = etree.parse(src_path)
    root = tree.getroot()
    rows = []
    for g, row in _group_rows(root):
        rows.append(row)
        if row.already_labeled or not row.mapped:
            continue
        g.set(_LABEL, row.layer)
        g.set(_GROUPMODE, "layer")
    tree.write(dst_path, xml_declaration=True, encoding="UTF-8")
    return rows


# --------------------------------------------------------------------------- #
# Output validation / repair -- the guard against today's truncation bug.
# --------------------------------------------------------------------------- #
def _sexpr_balance(text):
    """Return (depth, min_depth, unterminated_string) for an s-expression,
    counting parens but IGNORING any inside double-quoted strings (the descr
    line holds a Windows path, etc.). depth>0 => missing closers."""
    depth = min_depth = 0
    in_str = esc = False
    for ch in text:
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth < min_depth:
                min_depth = depth
    return depth, min_depth, in_str


def validate_and_repair(path):
    """Ensure a .kicad_mod is a balanced s-expression; repair a missing final
    ')' in place. Returns (missing_closers_added). Raises ValueError if the
    file is unbalanced in a way we can't safely fix."""
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        text = fh.read()
    head = text.lstrip()
    if not (head.startswith("(module") or head.startswith("(footprint")):
        raise ValueError("output is not a KiCad footprint (no (module/(footprint)")
    depth, min_depth, in_str = _sexpr_balance(text)
    if in_str:
        raise ValueError("output ends inside an unterminated string -- corrupt")
    if min_depth < 0:
        raise ValueError("output has an unexpected ')' -- corrupt, not repairing")
    if depth == 0:
        return 0
    # depth>0: svg2mod stopped short of the closing paren(s). Safe to finish it.
    with open(path, "a", encoding="utf-8") as fh:
        if not text.endswith("\n"):
            fh.write("\n")
        fh.write(")" * depth + "\n")
    return depth


# --------------------------------------------------------------------------- #
# Conversion
# --------------------------------------------------------------------------- #
class _LogCapture(logging.Handler):
    """Pipe svg2mod's WARNINGS into a progress callback and collect them for
    the summary. We deliberately drop svg2mod's INFO per-path chatter ("Writing
    Path with N points", thousands of lines) -- milestone progress comes from
    convert()'s own say() calls, so the log stays readable in CLI and GUI."""

    def __init__(self, progress):
        super().__init__(level=logging.WARNING)
        self.progress = progress
        self.warnings = []

    def emit(self, record):
        msg = record.getMessage()
        self.warnings.append(msg)
        if self.progress:
            self.progress(f"  [svg2mod] {msg}")


class ConvertResult:
    def __init__(self, output, rows, warnings, closers_added, crash=None,
                 holes_skipped=0):
        self.output = output
        self.rows = rows                       # list[LayerRow]
        self.warnings = warnings
        self.closers_added = closers_added     # >0 means we repaired truncation
        self.crash = crash                     # svg2mod exception msg, or None
        self.holes_skipped = holes_skipped     # unbridgeable holes left filled

    @property
    def incomplete(self):
        """True if svg2mod crashed mid-write: the file was repaired so it
        imports, but geometry after the crash point is MISSING."""
        return self.crash is not None

    @property
    def mapped(self):
        return [r for r in self.rows if r.mapped]

    @property
    def unmatched(self):
        return [r for r in self.rows if not r.mapped]


def _draw_to_path_d(items):
    """Turn one PyMuPDF drawing's items (lines/curves/rects/quads, in PDF
    points, top-left origin like SVG) into an SVG path 'd' string. Subpath
    breaks become new M commands so holes survive; each subpath is closed."""
    parts = []
    last = None
    open_sub = False

    def moveto(p):
        nonlocal open_sub
        if open_sub:
            parts.append("Z")
        parts.append(f"M{p.x:.4f} {p.y:.4f}")
        open_sub = True

    def near(a, b):
        return a is not None and abs(a.x - b.x) < 1e-4 and abs(a.y - b.y) < 1e-4

    for it in items:
        op = it[0]
        if op == "l":
            p1, p2 = it[1], it[2]
            if not near(last, p1):
                moveto(p1)
            parts.append(f"L{p2.x:.4f} {p2.y:.4f}")
            last = p2
        elif op == "c":
            p1, p2, p3, p4 = it[1], it[2], it[3], it[4]
            if not near(last, p1):
                moveto(p1)
            parts.append(f"C{p2.x:.4f} {p2.y:.4f} {p3.x:.4f} {p3.y:.4f} "
                         f"{p4.x:.4f} {p4.y:.4f}")
            last = p4
        elif op == "re":
            r = it[1]
            pts = [(r.x0, r.y0), (r.x1, r.y0), (r.x1, r.y1), (r.x0, r.y1)]
            if len(it) > 2 and it[2] == -1:        # preserve winding for holes
                pts = [pts[0], pts[3], pts[2], pts[1]]
            if open_sub:
                parts.append("Z")
            parts.append("M%.4f %.4f" % pts[0])
            for x, y in pts[1:]:
                parts.append("L%.4f %.4f" % (x, y))
            parts.append("Z")
            open_sub = False
            last = None
        elif op == "qu":
            q = it[1]
            qp = [q.ul, q.ur, q.lr, q.ll]
            if open_sub:
                parts.append("Z")
            parts.append(f"M{qp[0].x:.4f} {qp[0].y:.4f}")
            for p in qp[1:]:
                parts.append(f"L{p.x:.4f} {p.y:.4f}")
            parts.append("Z")
            open_sub = False
            last = None
    if open_sub:
        parts.append("Z")
    return " ".join(parts)


def ai_to_svg(ai_path, svg_path, progress=None):
    """Read a native .ai DIRECTLY (no Illustrator) and write an svg2mod-ready
    SVG. A modern .ai is a PDF whose top-level Illustrator layers are PDF
    optional-content groups; PyMuPDF tags each vector path with its layer name,
    so we extract the filled, layer-tagged paths and group them by layer.

    Geometry is emitted in PDF points with a physical mm width/height, so the
    output size is correct regardless of the DPI setting. Returns svg_path.

    Requires: PyMuPDF (pip install pymupdf), the .ai saved PDF-compatible (the
    Illustrator default), and any text already outlined (Type > Create Outlines).
    """
    say = progress or (lambda m: None)
    try:
        import fitz                             # PyMuPDF
    except ImportError as e:
        raise RuntimeError(
            "Reading .ai needs PyMuPDF. Install it with: pip install pymupdf"
        ) from e

    say("Reading .ai (PDF layers) with PyMuPDF ...")
    doc = fitz.open(ai_path)
    try:
        page = doc[0]
        rect = page.rect
        # Warn if there's live (un-outlined) text -- it won't be converted.
        try:
            td = page.get_text("rawdict")
            spans = sum(len(l["spans"]) for b in td["blocks"]
                        if b.get("type") == 0 for l in b.get("lines", []))
            if spans:
                say(f"WARNING: {spans} live text span(s) found -- outline text "
                    "in Illustrator (Type > Create Outlines) or it is dropped.")
        except Exception:
            pass

        # Collect filled paths grouped by their layer (OCG) name.
        from collections import OrderedDict
        layers = OrderedDict()
        stroked = 0
        for d in page.get_drawings():
            if d.get("fill") is None:            # svg2mod converts fills, not strokes
                # Flag stroke-only shapes on a named layer, so an outline that
                # vanishes is explained rather than silently dropped.
                if d.get("color") is not None and d.get("layer"):
                    stroked += 1
                continue
            layer = d.get("layer")
            if not layer:
                continue
            dd = _draw_to_path_d(d["items"])
            if not dd:
                continue
            rule = "evenodd" if d.get("even_odd") else "nonzero"
            layers.setdefault(layer, []).append((dd, rule))
        if stroked:
            say(f"WARNING: {stroked} stroked shape(s) with no fill were skipped. "
                "Give them a fill, or in Illustrator use Object > Path > Outline "
                "Stroke, then re-export.")
    finally:
        doc.close()

    if not layers:
        raise RuntimeError(
            "No layer-tagged filled paths found in the .ai. Make sure it was "
            "saved PDF-compatible and the shapes are filled.")

    w_mm = rect.width * 25.4 / 72.0
    h_mm = rect.height * 25.4 / 72.0
    from xml.sax.saxutils import quoteattr
    lines = ['<?xml version="1.0" encoding="UTF-8"?>',
             f'<svg xmlns="http://www.w3.org/2000/svg" width="{w_mm:.4f}mm" '
             f'height="{h_mm:.4f}mm" viewBox="0 0 {rect.width:.4f} '
             f'{rect.height:.4f}">']
    for layer, paths in layers.items():
        lines.append(f'  <g id={quoteattr(layer)}>')
        for dd, rule in paths:
            lines.append(f'    <path fill="#000000" fill-rule="{rule}" '
                         f'd="{dd}"/>')
        lines.append("  </g>")
    lines.append("</svg>")
    with open(svg_path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))
    say(f"Extracted {sum(len(p) for p in layers.values())} path(s) across "
        f"{len(layers)} layer(s) from the .ai.")
    return svg_path


def convert(input_svg, output=None, precision=1.0, module_name="Vector2KiCad",
            module_value="G***", center=False, dpi=None, pads=False,
            dump_svg=None, progress=None):
    """Convert an Illustrator .ai or an SVG to a KiCad .kicad_mod. Returns a
    ConvertResult.

    A .ai input is first exported to SVG by driving Illustrator (see ai_to_svg).
    dpi: None auto-detects from the SVG's origin (Illustrator 72 / Inkscape 96);
         pass an int to force it.
    progress: optional callable(str) for live log lines (used by the GUI).
    dump_svg: optional path to keep the normalized intermediate SVG for
              inspection (otherwise it goes to a temp file and is deleted).
    """
    def say(m):
        if progress:
            progress(m)

    note = svg2mod_version_note()
    if note:
        say("WARNING: " + note)

    if not os.path.isfile(input_svg):
        raise FileNotFoundError(input_svg)
    if output is None:
        output = os.path.splitext(input_svg)[0] + ".kicad_mod"
    elif not output.lower().endswith(".kicad_mod"):
        output += ".kicad_mod"

    # .ai input: have Illustrator export an SVG we then treat as the source.
    ai_tmp = None
    if input_svg.lower().endswith(".ai"):
        fd, ai_tmp = tempfile.mkstemp(suffix=".svg", prefix="vector2kicad_ai_")
        os.close(fd)
        ai_to_svg(input_svg, ai_tmp, progress=say)
        source = ai_tmp
    else:
        source = input_svg

    if dpi is None:
        dpi, dsrc = detect_dpi(source)
        say(f"Auto DPI: {dpi} ({dsrc})")

    # 1) Normalize Illustrator layers -> inkscape labels svg2mod understands.
    say(f"Normalizing layers in {os.path.basename(input_svg)} ...")
    if dump_svg:
        norm_path, keep = dump_svg, True
    else:
        fd, norm_path = tempfile.mkstemp(suffix=".svg", prefix="vector2kicad_")
        os.close(fd)
        keep = False
    try:
        rows = normalize_svg(source, norm_path)
        mapped = [r for r in rows if r.mapped]
        if not mapped:
            raise ValueError(
                "No groups matched a KiCad layer. Name your Illustrator layers "
                "after KiCad layers (Edge.Cuts, F.SilkS, F.Mask, ...).")
        for r in mapped:
            tag = " (from label)" if r.already_labeled else ""
            say(f"  layer: {r.name!r} -> {r.layer}{tag}")
        for r in rows:
            if not r.mapped:
                say(f"  ignored group: {r.name!r} (no KiCad layer)")

        # 2) Drive svg2mod in-process, capturing its log. svg2mod can crash
        #    mid-write on some panels (a geometry IndexError), leaving a file
        #    truncated by its final ')'. We don't let that become an unhandled
        #    error: we repair the partial file so it still imports, but flag the
        #    result as incomplete so the caller warns loudly.
        say(f"Converting with svg2mod (precision={precision}, dpi={dpi}) ...")
        cap = _LogCapture(say)
        svg2mod_logger.addHandler(cap)
        crash = None
        exported = None          # so the except block is safe if import() raises
        try:
            imported = Svg2ModImport(norm_path, module_name, module_value,
                                     ignore_hidden=False, force_layer=None)
            exported = Svg2ModExportLatest(
                imported, output, center, 1.0, precision, dpi=dpi, pads=pads)
            cmdline = (f"vector2kicad '{input_svg}' --precision {precision} "
                       f"--dpi {dpi}")
            exported.write(cmdline)
        except Exception as e:                 # noqa: BLE001 -- svg2mod internal
            crash = f"{type(e).__name__}: {e}"
            say(f"!! svg2mod crashed mid-write: {crash}")
            # Close the exporter's file handle so we can repair what it wrote.
            try:
                if getattr(exported, "output_file", None):
                    exported.output_file.close()
            except Exception:
                pass
        finally:
            svg2mod_logger.removeHandler(cap)
    finally:
        if not keep and os.path.exists(norm_path):
            try:
                os.remove(norm_path)
            except OSError:
                pass
        if ai_tmp and os.path.exists(ai_tmp):
            try:
                os.remove(ai_tmp)
            except OSError:
                pass

    if crash and not os.path.isfile(output):
        raise RuntimeError(f"svg2mod failed before writing any output: {crash}")

    # 3) Guarantee the output imports -- repair svg2mod's truncation if any.
    closers = validate_and_repair(output)
    if closers:
        say(f"Repaired unbalanced output: appended {closers} closing paren(s).")
    else:
        say("Output s-expression is balanced.")
    if crash:
        say("WARNING: output is INCOMPLETE -- svg2mod crashed before emitting "
            "all shapes. It will import, but geometry is likely missing.")
    skipped = sum(1 for w in cap.warnings if w.startswith(_SKIP_MSG))
    if skipped:
        say(f"Note: {skipped} hole(s) could not be bridged and stay filled "
            "(the rest of the panel is complete).")
    say(f"Wrote {output}")
    return ConvertResult(output, rows, cap.warnings, closers, crash=crash,
                         holes_skipped=skipped)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def main(argv=None):
    p = argparse.ArgumentParser(
        description="Convert an Illustrator/SVG panel directly to a KiCad "
                    "footprint (no Inkscape step).")
    p.add_argument("input", metavar="IN.ai|IN.svg",
                   help="input Illustrator .ai (exported via Illustrator) or SVG")
    p.add_argument("-o", "--output", metavar="OUT.kicad_mod",
                   help="output footprint (default: alongside the input)")
    p.add_argument("-p", "--precision", type=float, default=1.0,
                   help="curve flattening: approx segment length in SVG px "
                        "(smaller = smoother/bigger). Default 1.0")
    p.add_argument("-d", "--dpi", type=int, default=None,
                   help="SVG DPI for px->mm (default: auto -- Illustrator 72, "
                        "Inkscape 96). Set only to override the auto-detection.")
    p.add_argument("--name", default="Vector2KiCad", help="footprint name")
    p.add_argument("--value", default="G***", help="footprint value")
    p.add_argument("-c", "--center", action="store_true",
                   help="center the footprint on its bounding box")
    p.add_argument("--dump-svg", metavar="PATH",
                   help="keep the normalized intermediate SVG here")
    p.add_argument("--list-layers", action="store_true",
                   help="just report how each group maps to a KiCad layer")
    args = p.parse_args(argv)

    if args.list_layers:
        rows = scan_layers(args.input)
        if not rows:
            print("No layer-like groups found.")
            return 0
        width = max(len(r.name) for r in rows)
        for r in rows:
            where = "label" if r.already_labeled else ("data-name" if r.data_name
                                                        else "id")
            dest = r.layer if r.mapped else "-- (ignored)"
            print(f"  {r.name:<{width}}  [{where}]  ->  {dest}")
        mapped = sum(r.mapped for r in rows)
        print(f"\n{mapped} of {len(rows)} group(s) map to a KiCad layer.")
        return 0

    try:
        res = convert(args.input, args.output, precision=args.precision,
                      module_name=args.name, module_value=args.value,
                      center=args.center, dpi=args.dpi, dump_svg=args.dump_svg,
                      progress=print)
    except Exception as e:
        print(f"ERROR: {type(e).__name__}: {e}", file=sys.stderr)
        return 1
    if res.incomplete:
        print(f"\nINCOMPLETE: {res.output}")
        print("  svg2mod crashed mid-write; the file imports but is MISSING "
              "geometry.")
        print(f"  cause: {res.crash}")
        return 2
    print(f"\nDone: {len(res.mapped)} layer(s) -> {res.output}")
    if res.warnings:
        print(f"({len(res.warnings)} svg2mod warning(s) -- see log above.)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
