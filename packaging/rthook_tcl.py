# -*- coding: utf-8 -*-
"""PyInstaller runtime hook: point Tcl/Tk at the bundled data folders."""
import os
import sys

_base = getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
os.environ.setdefault("TCL_LIBRARY", os.path.join(_base, "tcl8.6"))
os.environ.setdefault("TK_LIBRARY", os.path.join(_base, "tk8.6"))
