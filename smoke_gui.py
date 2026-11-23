# -*- coding: utf-8 -*-
"""End-to-end GUI test: open one or two real libraries, select references,
export them through the real ExportDialog (worker thread included) and verify
the folder on disk.

Usage:
    set ENDNOTE_LIBRARY=D:\\path\\My EndNote Library.enl
    python smoke_gui.py

It uses the library from $ENDNOTE_LIBRARY (or the one remembered by the GUI)
and, if EndNote's bundled sample library exists, adds it as a second library to
exercise the "export from several libraries into one folder" path.
"""

import os
import shutil
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import tkinter as tk

import app as appmod
from app import App, ExportDialog, enable_dpi_awareness
from paths import default_library, default_sample_enlx

LIB1 = default_library()
LIB2 = default_sample_enlx()
GROUP_NAME = os.environ.get("ENDNOTE_GROUP", "")


def pump(root, seconds):
    end = time.time() + seconds
    while time.time() < end:
        root.update()
        time.sleep(0.02)


def pick_group(lib):
    """Use $ENDNOTE_GROUP if set, else the group with the most references."""
    if GROUP_NAME:
        match = next((g for g in lib.custom_groups if g.name == GROUP_NAME), None)
        if match is not None:
            return match
        print("warning: group %r not found, falling back to the biggest one" % GROUP_NAME)
    groups = [g for g in lib.custom_groups if g.member_ids]
    return max(groups, key=lambda g: len(g.member_ids)) if groups else None


def main():
    if not LIB1 or not os.path.exists(LIB1):
        print("no EndNote library found; set ENDNOTE_LIBRARY to a .enl/.enlx file")
        return 2
    out_dir = tempfile.mkdtemp(prefix="en2ima_smoke_")
    enable_dpi_awareness()
    root = tk.Tk()
    root.withdraw()
    root.tk.call("tk", "scaling", root.winfo_fpixels("1i") / 72.0)
    app = App(root)
    lib1 = app.add_library_path(LIB1)
    pump(root, 0.6)

    group = pick_group(lib1)
    if group is None:
        print("library has no non-empty group to test with")
        app.on_close()
        return 2
    app.show_view(os.path.abspath(LIB1), "group", group.id)
    pump(root, 0.4)
    print("group %r: %d refs, table rows %d"
          % (group.name, len(group.member_ids), len(app.row_meta)))
    app.select_all_visible()
    pump(root, 0.3)
    print("library 1 selected:", len(app.sel[os.path.abspath(LIB1)]))

    if os.path.exists(LIB2):
        lib2 = app.add_library_path(LIB2)
        pump(root, 0.4)
        app.show_view(os.path.abspath(LIB2), "all", None)
        pump(root, 0.4)
        with_pdf_ids = {r.id for r in lib2.active_references if r.has_pdf}
        iids = [iid for iid, (path, ref) in app.row_meta.items() if ref.id in with_pdf_ids]
        app.table.selection_set(iids)
        app._on_table_select()
        pump(root, 0.3)
        print("library 2 refs with pdf: %d, selected: %d"
              % (len(with_pdf_ids), len(app.sel[os.path.abspath(LIB2)])))
        app.show_view(os.path.abspath(LIB1), "group", group.id)
        pump(root, 0.3)

    items, no_pdf = app.collect_items()
    pdf_total = sum(len([a for a in it.reference.pdfs if a.exists]) for it in items)
    print("collect_items: %d refs, %d pdfs, %d without pdf" % (len(items), pdf_total, no_pdf))

    app.out_dir.set(out_dir)
    try:
        appmod.messagebox.askokcancel = lambda *a, **k: True
        dialog = ExportDialog(app, items, no_pdf)
        dialog.out_var.set(out_dir)
        dialog._start()
        deadline = time.time() + 180
        while dialog.result is None and time.time() < deadline:
            root.update()
            time.sleep(0.03)
        root.update()
        if dialog.result is None:
            print("EXPORT DID NOT FINISH")
            app.on_close()
            return 1
        result = dialog.result
        print("RESULT: exported=%d, bytes=%d, no_pdf=%d, missing=%d, errors=%d, seconds=%.2f"
              % (result.count, result.total_bytes, len(result.no_pdf),
                 len(result.missing), len(result.errors), result.seconds))
        print("out_dir:", result.out_dir)
        assert result.count == pdf_total, "expected %d files, got %d" % (pdf_total, result.count)
        pdfs = [n for n in sorted(os.listdir(out_dir)) if n.lower().endswith(".pdf")]
        self_check = all(
            open(os.path.join(out_dir, n), "rb").read(4) == b"%PDF" for n in pdfs
        )
        print("pdf files on disk:", len(pdfs), "| all valid PDFs:", self_check)
        print("libraries exported:", sorted({f.library for f in result.files}))
        ok = len(pdfs) == pdf_total and self_check and not result.errors
        print("SMOKE_OK" if ok else "SMOKE_FAILED")
        return 0 if ok else 1
    finally:
        app.on_close()
        shutil.rmtree(out_dir, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
