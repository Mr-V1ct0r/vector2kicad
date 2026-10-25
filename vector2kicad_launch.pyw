#!/usr/bin/env pythonw
r"""
Desktop-shortcut launcher for the vector2kicad GUI.

A .pyw runs under pythonw.exe, so double-clicking it (or a desktop shortcut to
it) opens the GUI with no console window. It just launches the live
vector2kicad_gui, so nothing needs rebuilding when the tool changes.

Make a shortcut:
  Right-click this file -> Send to -> Desktop (create shortcut), then rename the
  shortcut "Vector2KiCad". If double-clicking opens an editor instead of
  running, set the shortcut target explicitly to:
    <your-python>\pythonw.exe "<this folder>\vector2kicad_launch.pyw"

Needs svg2mod and lxml (pip install svg2mod lxml). Any startup error is shown
in a dialog rather than failing silently (there's no console under pythonw).
"""

if __name__ == "__main__":
    try:
        import os
        import sys
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from vector2kicad_gui import main
        main()
    except (Exception, SystemExit):
        import traceback
        try:
            import tkinter.messagebox as mb
            mb.showerror("vector2kicad failed to start", traceback.format_exc())
        except Exception:
            pass
        raise
