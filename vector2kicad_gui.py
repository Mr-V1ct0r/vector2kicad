#!/usr/bin/env python3
"""
vector2kicad GUI (Tkinter front-end for vector2kicad.py).

Pick an Illustrator/Inkscape SVG, see how its layers map to KiCad layers, set
precision/DPI, hit Convert, and get a .kicad_mod -- no Inkscape round-trip. The
conversion runs on a worker thread and reports progress through a queue, so the
window stays responsive on big panels (svg2mod can take a minute or two). The
engine (vector2kicad.py) owns all the conversion logic; this is only a
front-end, matching the split used by the Cloct cal tool.

Run:  python vector2kicad_gui.py        (needs svg2mod + lxml + Tk)
"""

import json
import os
import queue
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import ttk, filedialog, messagebox


VERSION = "1.0"


def _open_in_file_manager(path):
    """Open a folder in the OS file manager (Explorer, Finder, or the Linux
    default). Cross-platform replacement for the Windows-only os.startfile."""
    if sys.platform.startswith("win"):
        os.startfile(path)                               # noqa: W605 (Windows)
    elif sys.platform == "darwin":
        subprocess.run(["open", path], check=False)
    else:
        subprocess.run(["xdg-open", path], check=False)


def _resource_dir():
    """Folder that holds bundled resources (the icons). Handles a PyInstaller
    build (files are unpacked to sys._MEIPASS) and a normal source checkout."""
    if getattr(sys, "frozen", False):
        return getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def selfcheck():
    """Print the version and confirm every dependency is importable. Used to
    verify a packaged build, and as a troubleshooting aid for a user whose app
    will not start. Returns 0 if everything is present, 1 otherwise. Writes the
    same report to a temp file so it is readable even from a windowed build
    whose stdout is not attached to a console."""
    import importlib.metadata as _md
    import platform
    import tempfile

    def _ver(mod, m):
        if mod == "tkinter":
            return str(getattr(tk, "TkVersion", "?"))
        v = getattr(m, "__version__", None) or getattr(m, "VersionBind", None)
        if v:
            return str(v)
        for dist in (mod, "pymupdf" if mod == "fitz" else mod):
            try:
                return _md.version(dist)
            except _md.PackageNotFoundError:
                continue
        return "?"

    lines = [f"Vector2KiCad {VERSION}",
             f"Python   {platform.python_version()}   OK"]
    ok = True
    for mod, note in (("tkinter", ""), ("svg2mod", ""), ("lxml", ""),
                      ("fitz", "(.ai support)")):
        try:
            m = __import__(mod)
            ver = _ver(mod, m)
            lines.append(f"{mod:8} {ver!s:12} OK   {note}")
        except Exception as e:                           # noqa: BLE001
            ok = False
            lines.append(f"{mod:8} {'':12} MISSING: {e}")
    report = "\n".join(lines)
    print(report)
    try:
        with open(os.path.join(tempfile.gettempdir(),
                               "vector2kicad_selfcheck.txt"),
                  "w", encoding="utf-8") as fh:
            fh.write(report + "\n")
    except OSError:
        pass
    return 0 if ok else 1

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from vector2kicad import convert, scan_layers, detect_dpi, svg2mod_version_note


# Per-user settings, kept OUT of the (possibly shared) project folder so each
# user has their own and the repo stays clean.
_SETTINGS_DEFAULTS = {
    "precision": "1.0", "dpi": "Auto", "center": False,
    "name": "Vector2KiCad", "value": "G***", "keep_norm_svg": False,
    "last_in_dir": "", "last_out_dir": "",
}


def _settings_path():
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    return os.path.join(base, "Vector2KiCad", "settings.json")


# Short, on hover help for each control. Kept free of em and en dashes.
OPTION_HELP = {
    "input": "The Illustrator .ai or SVG panel to convert. For a .ai, save it "
             "PDF compatible and outline any text first.",
    "output": "Where to write the .kicad_mod. Defaults to the same name and "
              "folder as the input.",
    "precision": "How finely curves are flattened into straight segments, in "
                 "SVG pixels. Smaller gives smoother curves and a larger file. "
                 "1.0 is a good default. Try 0.5 if small shapes or the holes "
                 "in letters look rough.",
    "dpi": "How SVG coordinates are turned into mm. Auto reads it from the file "
           "(Illustrator is 72, Inkscape is 96). Leave on Auto unless the panel "
           "comes out the wrong size. Ignored for a .ai, which is read in mm.",
    "center": "Shifts the footprint so its bounding box is centered on the "
              "origin. Leave it off to keep the artwork where you drew it.",
    "name": "The footprint name saved inside the file.",
    "value": "The footprint value field saved inside the file.",
    "keepsvg": "Also writes the intermediate normalized SVG next to the output "
               "so you can inspect it. Normally off.",
    "scan": "Re-read the layers for the current file and preview the mapping, "
            "without converting. Picking a file scans it for you, so use this to "
            "refresh after you re-export the art from Illustrator, or after "
            "typing a path by hand (you can also press Enter in the Input box).",
    "convert": "Run the conversion and write the .kicad_mod.",
    "openfolder": "Open the folder that holds the output file.",
}

# Longer walk through for the Help popup. Each item is (heading, body); a blank
# heading is a plain paragraph. No em or en dashes, no double hyphens.
HELP_SECTIONS = [
    ("", "Turn an Illustrator .ai or an SVG panel into a KiCad footprint. Name "
         "your Illustrator layers after KiCad layers (Edge.Cuts, F.SilkS, "
         "F.Mask, F.Cu, B.Cu, and so on), use filled shapes, and outline any "
         "text. Then pick the file and press CONVERT."),
    ("Files", None),
    ("Input", OPTION_HELP["input"]),
    ("KiCad out", OPTION_HELP["output"]),
    ("Options", None),
    ("Precision (px)", OPTION_HELP["precision"]),
    ("DPI", OPTION_HELP["dpi"]),
    ("Center on bbox", OPTION_HELP["center"]),
    ("Name", OPTION_HELP["name"]),
    ("Value", OPTION_HELP["value"]),
    ("Keep normalized SVG", OPTION_HELP["keepsvg"]),
    ("Buttons", None),
    ("SCAN LAYERS", OPTION_HELP["scan"]),
    ("CONVERT", OPTION_HELP["convert"]),
    ("Open output folder", OPTION_HELP["openfolder"]),
    ("Layer table", "Green rows map to a KiCad layer and will be converted. "
                    "Grey rows are ignored, because their name is not a KiCad "
                    "layer. Rename those layers in Illustrator if you want them."),
    ("Supported layers", "Name an Illustrator layer after any of these and it "
                    "converts: Edge.Cuts; F.Cu and B.Cu; F.SilkS and B.SilkS; "
                    "F.Mask and B.Mask; F.Paste and B.Paste; F.Adhes and "
                    "B.Adhes; F.Fab and B.Fab; F.CrtYd and B.CrtYd; Dwgs.User, "
                    "Cmts.User, Eco1.User, Eco2.User; Drill.Cu and Drill.Mech "
                    "(round holes only, drawn as circles); and any name ending "
                    "in .Keepout, such as F.Keepout (KiCad 6 or newer). Copper "
                    "layers also support pad options. See the README for those "
                    "advanced features."),
    ("Holes and windows", "Holes and cutout windows come from filled shapes "
                          "that have holes, such as a copper pour with openings. "
                          "Cut them with Pathfinder Minus Front as usual. The "
                          "tool sorts out the path direction for you, so the "
                          "holes come through even when Illustrator winds them "
                          "the same way as the outline."),
    ("Tips", "If a curve or a letter hole looks rough, lower Precision to 0.5. "
             "If the whole panel is the wrong size, set DPI by hand. Your "
             "options and last used folders are remembered between runs."),
    ("Credits", f"Vector2KiCad v{VERSION} is made by 00 Modular. It is built on "
                "svg2mod (GPL v2 or later), which does all the core geometry "
                "work. See the README and LICENSE for the full details."),
]


class Tooltip:
    """Small hover label for a widget. Appears after a short delay, hides on
    leave or click. One per widget."""

    def __init__(self, widget, text, delay=500):
        self.widget = widget
        self.text = text
        self.delay = delay
        self.tip = None
        self._after = None
        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<ButtonPress>", self._hide, add="+")

    def _schedule(self, _e=None):
        self._cancel()
        self._after = self.widget.after(self.delay, self._show)

    def _cancel(self):
        if self._after:
            self.widget.after_cancel(self._after)
            self._after = None

    def _show(self):
        if self.tip or not self.text:
            return
        x = self.widget.winfo_rootx() + 18
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 4
        self.tip = tw = tk.Toplevel(self.widget)
        tw.wm_overrideredirect(True)
        tw.wm_geometry(f"+{x}+{y}")
        tk.Label(tw, text=self.text, justify="left", background="#ffffe0",
                 relief="solid", borderwidth=1, font=("Segoe UI", 9),
                 wraplength=360, padx=6, pady=4).pack()

    def _hide(self, _e=None):
        self._cancel()
        if self.tip:
            self.tip.destroy()
            self.tip = None


class PanelGui:
    def __init__(self, root):
        self.root = root
        root.title("Vector2KiCad")
        self._apply_icon()
        self.q = queue.Queue()
        self.worker = None
        self.cfg = self._load_settings()
        self._build()
        self._apply_settings()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.after(60, self._poll)
        note = svg2mod_version_note()
        if note:
            self.root.after(150, lambda: self._svg2mod_warn(note))

    def _svg2mod_warn(self, note):
        """Surface a wrong-svg2mod-version warning once at startup."""
        self.log("WARNING: " + note)
        self.status.config(text="svg2mod version mismatch (see log)")
        messagebox.showwarning("svg2mod version", note)

    # ---- window icon (your branding) ----
    def _apply_icon(self):
        """Set the window icon(s).

        On Windows a window can carry two icons: a SMALL one (title-bar corner)
        and a BIG one (taskbar button and Alt-Tab). Tk's iconbitmap sets both to
        the same image, so we set the base with iconbitmap (the V2K text mark,
        readable at 16px) and then override just the BIG icon with the badge via
        WM_SETICON. Result: V2K text in the title bar, full badge on the taskbar.
        On macOS and Linux (one icon) we use the badge via iconphoto; on macOS
        the Dock icon is controlled by the app bundle, so it may not change.
        Silent no-op if an icon file is missing."""
        here = _resource_dir()
        ico = os.path.join(here, "vector2kicad.ico")        # V2K text (small)
        badge = os.path.join(here, "vector2kicad-badge.ico")  # badge (big)
        png = os.path.join(here, "vector2kicad.png")
        if sys.platform.startswith("win"):
            try:
                if os.path.exists(ico):
                    self.root.iconbitmap(ico)
            except Exception:
                pass
            # Defer the big-icon override until the native window exists.
            self.root.after(0, lambda: self._set_taskbar_icon(ico, badge))
            return
        try:
            png_badge = os.path.join(here, "vector2kicad-logo-48.png")
            use = png_badge if os.path.exists(png_badge) else png
            if os.path.exists(use):
                self._icon_img = tk.PhotoImage(file=use)   # keep a reference
                self.root.iconphoto(True, self._icon_img)
        except Exception:
            pass

    def _set_taskbar_icon(self, small_ico, big_ico):
        """Windows only: make the taskbar/Alt-Tab (BIG) icon the badge while the
        title-bar (SMALL) icon stays the V2K text mark. No-op on any failure."""
        try:
            import ctypes
            u = ctypes.windll.user32
            WM_SETICON, ICON_SMALL, ICON_BIG = 0x0080, 0, 1
            IMAGE_ICON, LR_LOADFROMFILE = 1, 0x0010
            hwnd = u.GetParent(self.root.winfo_id()) or self.root.winfo_id()

            def load(path, sz):
                if not os.path.exists(path):
                    return 0
                return u.LoadImageW(None, path, IMAGE_ICON, sz, sz,
                                    LR_LOADFROMFILE)

            self._hicon_small = load(small_ico, 16)
            self._hicon_big = load(big_ico, 32)
            if self._hicon_small:
                u.SendMessageW(hwnd, WM_SETICON, ICON_SMALL, self._hicon_small)
            if self._hicon_big:
                u.SendMessageW(hwnd, WM_SETICON, ICON_BIG, self._hicon_big)
        except Exception:
            pass

    # ---- settings persistence ----
    def _load_settings(self):
        cfg = dict(_SETTINGS_DEFAULTS)
        try:
            with open(_settings_path(), "r", encoding="utf-8") as fh:
                saved = json.load(fh)
            if isinstance(saved, dict):
                cfg.update({k: saved[k] for k in _SETTINGS_DEFAULTS if k in saved})
        except (OSError, ValueError):
            pass                                 # no/corrupt file -> defaults
        return cfg

    def _apply_settings(self):
        c = self.cfg
        self.e_prec.delete(0, "end"); self.e_prec.insert(0, str(c["precision"]))
        if c["dpi"] in ("Auto", "72", "96"):
            self.cb_dpi.set(c["dpi"])
        self.v_center.set(bool(c["center"]))
        self.e_name.delete(0, "end"); self.e_name.insert(0, str(c["name"]))
        self.e_value.delete(0, "end"); self.e_value.insert(0, str(c["value"]))
        self.v_keepsvg.set(bool(c["keep_norm_svg"]))

    def _collect_settings(self):
        self.cfg.update({
            "precision": self.e_prec.get().strip() or "1.0",
            "dpi": self.cb_dpi.get(),
            "center": bool(self.v_center.get()),
            "name": self.e_name.get().strip() or "Vector2KiCad",
            "value": self.e_value.get().strip() or "G***",
            "keep_norm_svg": bool(self.v_keepsvg.get()),
        })
        return self.cfg

    def _save_settings(self):
        try:
            p = _settings_path()
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "w", encoding="utf-8") as fh:
                json.dump(self._collect_settings(), fh, indent=2)
        except OSError:
            pass                                 # never let a save failure break the app

    def _on_close(self):
        self._save_settings()
        self.root.destroy()

    # ---- layout ----
    def _build(self):
        pad = {"padx": 6, "pady": 3}

        # Branding header: the full badge logo, next to the Vector2KiCad name
        # with a light "by 00 Modular" credit beneath. The simple V2K text mark
        # is the small window/title-bar icon (where the badge is too detailed to
        # read); the badge gets the room it needs here. Fuller credits
        # (including svg2mod) live in Help and the README.
        header = ttk.Frame(self.root)
        header.pack(fill="x", padx=10, pady=(10, 2))
        logo = os.path.join(_resource_dir(), "vector2kicad-logo-48.png")
        try:
            self._hdr_img = tk.PhotoImage(file=logo)
            tk.Label(header, image=self._hdr_img).pack(side="left")
        except Exception:
            tk.Label(header, text="V2K",
                     font=("Segoe UI", 22, "bold")).pack(side="left")
        namebox = ttk.Frame(header)
        namebox.pack(side="left", padx=12)
        ttk.Label(namebox, text="Vector2KiCad",
                  font=("Segoe UI", 14, "bold")).pack(anchor="w")
        ttk.Label(namebox, text=f"v{VERSION} · by 00 Modular",
                  font=("Segoe UI", 8),
                  foreground="#888888").pack(anchor="w")
        ttk.Separator(self.root, orient="horizontal").pack(fill="x", padx=8)

        files = ttk.LabelFrame(self.root, text="Files")
        files.pack(fill="x", **pad)
        ttk.Label(files, text="Input:").grid(row=0, column=0, sticky="e", **pad)
        self.e_in = ttk.Entry(files, width=58)
        self.e_in.grid(row=0, column=1, sticky="we", **pad)
        self.e_in.bind("<Return>", lambda _e: self.scan())   # typed/pasted path
        ttk.Button(files, text="Browse…", command=self.pick_in)\
            .grid(row=0, column=2, **pad)
        ttk.Label(files, text="KiCad out:").grid(row=1, column=0, sticky="e", **pad)
        self.e_out = ttk.Entry(files, width=58)
        self.e_out.grid(row=1, column=1, sticky="we", **pad)
        ttk.Button(files, text="Browse…", command=self.pick_out)\
            .grid(row=1, column=2, **pad)
        files.columnconfigure(1, weight=1)

        opt = ttk.LabelFrame(self.root, text="Options")
        opt.pack(fill="x", **pad)
        ttk.Label(opt, text="Precision (px):").grid(row=0, column=0, sticky="e", **pad)
        self.e_prec = ttk.Entry(opt, width=6)
        self.e_prec.insert(0, "1.0")
        self.e_prec.grid(row=0, column=1, sticky="w", **pad)
        ttk.Label(opt, text="DPI:").grid(row=0, column=2, sticky="e", **pad)
        self.cb_dpi = ttk.Combobox(opt, width=7, state="readonly",
                                   values=["Auto", "72", "96"])
        self.cb_dpi.set("Auto")
        self.cb_dpi.grid(row=0, column=3, sticky="w", **pad)
        self.v_center = tk.BooleanVar(value=False)
        self.chk_center = ttk.Checkbutton(opt, text="Center on bbox",
                                          variable=self.v_center)
        self.chk_center.grid(row=0, column=4, columnspan=2, sticky="w", **pad)
        ttk.Label(opt, text="Name:").grid(row=1, column=0, sticky="e", **pad)
        self.e_name = ttk.Entry(opt, width=16)
        self.e_name.insert(0, "Vector2KiCad")
        self.e_name.grid(row=1, column=1, columnspan=2, sticky="w", **pad)
        ttk.Label(opt, text="Value:").grid(row=1, column=3, sticky="e", **pad)
        self.e_value = ttk.Entry(opt, width=10)
        self.e_value.insert(0, "G***")
        self.e_value.grid(row=1, column=4, sticky="w", **pad)
        self.v_keepsvg = tk.BooleanVar(value=False)
        self.chk_keepsvg = ttk.Checkbutton(
            opt, text="Keep normalized SVG (as OUT.norm.svg)",
            variable=self.v_keepsvg)
        self.chk_keepsvg.grid(row=2, column=0, columnspan=5, sticky="w", **pad)

        btns = ttk.Frame(self.root)
        btns.pack(fill="x", **pad)
        self.b_scan = ttk.Button(btns, text="SCAN LAYERS", command=self.scan)
        self.b_scan.pack(side="left", padx=6)
        self.b_conv = ttk.Button(btns, text="CONVERT", command=self.convert)
        self.b_conv.pack(side="left", padx=6)
        self.b_openfolder = ttk.Button(btns, text="Open output folder",
                                       command=self.open_out_folder)
        self.b_openfolder.pack(side="left", padx=6)
        ttk.Button(btns, text="Help", command=self.show_help)\
            .pack(side="right", padx=6)

        cols = ("group", "source", "layer")
        self.tree = ttk.Treeview(self.root, columns=cols, show="headings",
                                 height=8)
        for c, w, a in (("group", 240, "w"), ("source", 90, "center"),
                        ("layer", 150, "center")):
            self.tree.heading(c, text={"group": "SVG GROUP",
                                       "source": "FROM",
                                       "layer": "KiCad LAYER"}[c])
            self.tree.column(c, width=w, anchor=a)
        self.tree.tag_configure("mapped", background="#d7f2d7")
        self.tree.tag_configure("ignored", background="#eeeeee")
        self.tree.pack(fill="both", expand=True, padx=6, pady=4)

        self.lbl_live = tk.Label(self.root, text="idle", anchor="w",
                                 font=("Consolas", 11), relief="sunken")
        self.lbl_live.pack(fill="x", padx=6, pady=2)

        self.txt = tk.Text(self.root, height=10, font=("Consolas", 9))
        self.txt.pack(fill="both", expand=True, padx=6, pady=4)
        self.status = ttk.Label(self.root, text="Ready", relief="sunken",
                                anchor="w")
        self.status.pack(fill="x")

        # Hover help on each control.
        for widget, key in ((self.e_in, "input"), (self.e_out, "output"),
                            (self.e_prec, "precision"), (self.cb_dpi, "dpi"),
                            (self.chk_center, "center"), (self.e_name, "name"),
                            (self.e_value, "value"),
                            (self.chk_keepsvg, "keepsvg"),
                            (self.b_scan, "scan"), (self.b_conv, "convert"),
                            (self.b_openfolder, "openfolder")):
            Tooltip(widget, OPTION_HELP[key])

    # ---- help popup ----
    def show_help(self):
        win = tk.Toplevel(self.root)
        win.title("vector2kicad help")
        win.transient(self.root)
        win.geometry("560x560")
        frame = ttk.Frame(win)
        frame.pack(fill="both", expand=True, padx=8, pady=8)
        sb = ttk.Scrollbar(frame)
        sb.pack(side="right", fill="y")
        txt = tk.Text(frame, wrap="word", font=("Segoe UI", 10),
                      yscrollcommand=sb.set, padx=8, pady=6)
        txt.pack(side="left", fill="both", expand=True)
        sb.config(command=txt.yview)
        txt.tag_configure("head", font=("Segoe UI", 11, "bold"),
                          spacing1=8, spacing3=2)
        txt.tag_configure("opt", font=("Segoe UI", 10, "bold"))
        for heading, body in HELP_SECTIONS:
            if body is None:                     # section header
                txt.insert("end", heading + "\n", "head")
            elif heading:                        # labeled option
                txt.insert("end", heading + ":  ", "opt")
                txt.insert("end", body + "\n\n")
            else:                                # plain paragraph
                txt.insert("end", body + "\n\n")
        txt.config(state="disabled")
        ttk.Button(win, text="Close", command=win.destroy)\
            .pack(pady=(0, 8))
        win.bind("<Escape>", lambda _e: win.destroy())

    # ---- helpers ----
    def log(self, msg):
        self.txt.insert("end", msg + "\n")
        self.txt.see("end")

    def _default_out(self, svg_path):
        return os.path.splitext(svg_path)[0] + ".kicad_mod"

    def pick_in(self):
        p = filedialog.askopenfilename(
            title="Choose an Illustrator .ai or an SVG",
            initialdir=self.cfg.get("last_in_dir") or None,
            filetypes=[("Illustrator / SVG", "*.ai *.svg"),
                       ("Illustrator", "*.ai"), ("SVG", "*.svg"),
                       ("All files", "*.*")])
        if not p:
            return
        self.cfg["last_in_dir"] = os.path.dirname(p)
        self.e_in.delete(0, "end")
        self.e_in.insert(0, p)
        self.e_out.delete(0, "end")
        self.e_out.insert(0, self._default_out(p))
        self.scan()

    def pick_out(self):
        p = filedialog.asksaveasfilename(
            title="Save KiCad footprint as", defaultextension=".kicad_mod",
            initialdir=self.cfg.get("last_out_dir") or None,
            filetypes=[("KiCad footprint", "*.kicad_mod"), ("All files", "*.*")])
        if p:
            self.cfg["last_out_dir"] = os.path.dirname(p)
            self.e_out.delete(0, "end")
            self.e_out.insert(0, p)

    def open_out_folder(self):
        out = self.e_out.get().strip()
        folder = os.path.dirname(out) if out else ""
        if folder and os.path.isdir(folder):
            try:
                _open_in_file_manager(folder)
            except Exception as e:
                messagebox.showerror("Output folder",
                                     f"Could not open the folder:\n{e}")
        else:
            messagebox.showinfo("Output folder", "Set an output path first.")

    def _fill_table(self, rows):
        for iid in self.tree.get_children():
            self.tree.delete(iid)
        for i, r in enumerate(rows):
            where = "label" if r.already_labeled else (
                "data-name" if r.data_name else "id")
            dest = r.layer if r.mapped else "— ignored"
            self.tree.insert("", "end", iid=str(i),
                             values=(r.name, where, dest),
                             tags=("mapped" if r.mapped else "ignored",))

    # ---- scan (quick, synchronous) ----
    def scan(self):
        src = self.e_in.get().strip()
        if not src or not os.path.isfile(src):
            messagebox.showerror("No file", "Pick a valid .ai or .svg first.")
            return
        is_ai = src.lower().endswith(".ai")
        try:
            rows = scan_layers(src)
        except Exception as e:
            messagebox.showerror("Scan failed", f"{type(e).__name__}: {e}")
            return
        self._fill_table(rows)
        mapped = sum(r.mapped for r in rows)
        if is_ai:
            note = "Illustrator .ai (read via PDF layers; size in mm, DPI unused)"
        else:
            dpi, dsrc = detect_dpi(src)
            note = f"{dsrc} ({dpi} DPI)"
            self.log(f"Detected source: {dsrc}  ->  {dpi} DPI"
                     + ("" if self.cb_dpi.get() != "Auto"
                        else "  (DPI = Auto will use this)"))
        self.status.config(text=f"{mapped} of {len(rows)} group(s) map to a "
                                f"KiCad layer.  Source: {note}")
        if mapped == 0:
            self.log("No groups matched a KiCad layer. Name your Illustrator "
                     "layers after KiCad layers (Edge.Cuts, F.SilkS, ...).")

    # ---- convert (worker thread) ----
    def _busy(self, on):
        state = "disabled" if on else "normal"
        self.b_conv.config(state=state)
        self.b_scan.config(state=state)

    def convert(self):
        if self.worker and self.worker.is_alive():
            return
        src = self.e_in.get().strip()
        if not src or not os.path.isfile(src):
            messagebox.showerror("No file", "Pick a valid .ai or .svg file first.")
            return
        out = self.e_out.get().strip() or self._default_out(src)
        try:
            prec = float(self.e_prec.get())
        except ValueError:
            messagebox.showerror("Bad option", "Precision must be a number.")
            return
        dpi_sel = self.cb_dpi.get()
        dpi = None if dpi_sel == "Auto" else int(dpi_sel)
        params = dict(
            input_svg=src, output=out, precision=prec, dpi=dpi,
            module_name=self.e_name.get().strip() or "Vector2KiCad",
            module_value=self.e_value.get().strip() or "G***",
            center=self.v_center.get(),
            dump_svg=(os.path.splitext(out)[0] + ".norm.svg"
                      if self.v_keepsvg.get() else None),
        )
        if out:
            self.cfg["last_out_dir"] = os.path.dirname(out)
        self._save_settings()                    # persist current options
        self.txt.delete("1.0", "end")
        self._busy(True)
        self.status.config(text="Converting…")
        self.lbl_live.config(text="working…")
        self.worker = threading.Thread(target=self._run, args=(params,),
                                       daemon=True)
        self.worker.start()

    def _run(self, params):
        try:
            res = convert(progress=lambda m: self.q.put({"t": "log", "msg": m}),
                          **params)
            self.q.put({"t": "done", "res": res})
        except Exception as e:
            self.q.put({"t": "error", "msg": f"{type(e).__name__}: {e}"})

    # ---- GUI thread: drain queue ----
    def _poll(self):
        try:
            while True:
                self._handle(self.q.get_nowait())
        except queue.Empty:
            pass
        self.root.after(60, self._poll)

    def _handle(self, ev):
        t = ev["t"]
        if t == "log":
            self.log(ev["msg"])
            self.lbl_live.config(text=ev["msg"][-90:])
        elif t == "done":
            res = ev["res"]
            self._fill_table(res.rows)
            self._busy(False)
            self.lbl_live.config(text="idle")
            if res.incomplete:
                self.status.config(text="INCOMPLETE – see log")
                messagebox.showwarning(
                    "Incomplete",
                    "svg2mod crashed mid-write. The file was repaired so it "
                    "imports, but geometry is missing.\n\n" + (res.crash or ""))
            else:
                extra = (f"  ({res.holes_skipped} hole(s) left filled)"
                         if res.holes_skipped else "")
                self.status.config(
                    text=f"Done: {len(res.mapped)} layer(s) → "
                         f"{os.path.basename(res.output)}{extra}")
                messagebox.showinfo(
                    "Done",
                    f"Wrote:\n{res.output}\n\n{len(res.mapped)} layer(s)"
                    + (f"\n{res.holes_skipped} hole(s) could not be bridged and "
                       "stay filled; the rest is complete."
                       if res.holes_skipped else ""))
        elif t == "error":
            self._busy(False)
            self.lbl_live.config(text="idle")
            self.status.config(text="Error")
            self.log("ERROR: " + ev["msg"])
            messagebox.showerror("Convert failed", ev["msg"])


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if "--selfcheck" in argv:
        return selfcheck()
    if "--version" in argv or "-V" in argv:
        print(f"Vector2KiCad {VERSION}")
        return 0
    if sys.platform.startswith("win"):
        # Give the app its own taskbar identity so Windows uses the window's
        # icon on the taskbar (not pythonw's) and groups it as Vector2KiCad.
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
                "Modular00.Vector2KiCad")
        except Exception:
            pass
    root = tk.Tk()
    PanelGui(root)
    root.minsize(680, 640)
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
