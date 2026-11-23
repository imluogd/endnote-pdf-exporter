# -*- coding: utf-8 -*-
"""endnote-pdf-exporter —— 从 EndNote 文献库导出选中文献的 PDF（图形界面）。

做一件事：把 EndNote 里你点选的文献所附的 PDF 复制到一个文件夹，
之后把这个文件夹导入 ima 知识库。
"""

from __future__ import annotations

import json
import os
import queue
import sys
import threading

import tkinter as tk
import tkinter.font as tkfont
from tkinter import filedialog, messagebox, ttk

from endnote_lib import EndNoteLibrary, LibraryError
from exporter import (
    ExportItem,
    ExportOptions,
    NamingOptions,
    export_items,
)

APP_NAME = "endnote-pdf-exporter"
APP_TITLE = APP_NAME
_APPDATA = os.environ.get("APPDATA") or os.path.expanduser("~")
CONFIG_DIR = os.path.join(_APPDATA, APP_NAME)
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")
# settings written by earlier builds lived here; read them once if needed
LEGACY_CONFIG_DIR = os.path.join(_APPDATA, "EndNote2ima")
LEGACY_CONFIG_PATH = os.path.join(LEGACY_CONFIG_DIR, "config.json")

LIB_FILETYPES = [
    ("EndNote 文献库", "*.enl"),
    ("EndNote 压缩库", "*.enlx"),
    ("所有文件", "*.*"),
]

HINT = (
    "① 点「＋ 添加文献库」，选择 EndNote 的 .enl 文件（EndNote 可以同时开着）\n"
    "② 左边点一个分组（或「全部文献」）\n"
    "③ 在右边按住 Ctrl / Shift 点选文献，或按 Ctrl+A 全选\n"
    "④ 点「导出选中 PDF…」选择文件夹，PDF 就会复制进去\n\n"
    "灰色条目表示该文献没有 PDF，导出时会自动跳过并提示。"
)


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def enable_dpi_awareness():
    try:
        import ctypes

        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


def load_config():
    for path in (CONFIG_PATH, LEGACY_CONFIG_PATH):
        try:
            with open(path, "r", encoding="utf-8-sig") as fh:
                data = json.load(fh)
                if isinstance(data, dict):
                    return data
        except (OSError, ValueError):
            continue
    return {}


def save_config(data):
    try:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        with open(CONFIG_PATH, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
    except OSError:
        pass


def human_size(n):
    value = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return ("%d B" % value) if unit == "B" else ("%.1f %s" % (value, unit))
        value /= 1024.0
    return "%.1f GB" % value


def default_out_dir():
    return os.path.join(os.path.expanduser("~"), "Documents", "EndNote导出PDF")


# --------------------------------------------------------------------------
# export dialog - deliberately tiny:  folder + file name + one button
# --------------------------------------------------------------------------

class ExportDialog(tk.Toplevel):

    def __init__(self, app, items, no_pdf_count):
        super().__init__(app.root)
        self.app = app
        self.items = items
        self.no_pdf_count = no_pdf_count
        self.result = None
        self._running = False
        self._queue = queue.Queue()

        self.title("导出 PDF")
        self.transient(app.root)
        self.resizable(False, False)
        self.columnconfigure(0, weight=1)

        body = ttk.Frame(self, padding=16)
        body.grid(row=0, column=0, sticky="nsew")
        body.columnconfigure(1, weight=1)

        n_refs = len({it.key for it in items})
        n_pdfs = sum(len([a for a in it.reference.pdfs if a.exists]) for it in items)
        text = "已选 %d 篇文献，共 %d 个 PDF。" % (n_refs, n_pdfs)
        if no_pdf_count:
            text += "另有 %d 篇没有 PDF，会跳过。" % no_pdf_count
        ttk.Label(body, text=text).grid(row=0, column=0, columnspan=3, sticky="w")

        ttk.Label(body, text="导出到").grid(row=1, column=0, sticky="w", pady=(16, 0))
        self.out_var = tk.StringVar(value=app.out_dir.get() or default_out_dir())
        ttk.Entry(body, textvariable=self.out_var, width=46).grid(
            row=1, column=1, sticky="ew", padx=(10, 8), pady=(16, 0)
        )
        ttk.Button(body, text="选择…", command=self._browse).grid(
            row=1, column=2, pady=(16, 0)
        )

        ttk.Label(body, text="文件名").grid(row=2, column=0, sticky="w", pady=(12, 0))
        self._scheme_keys = [key for key, _label in NamingOptions.SCHEMES]
        self._scheme_labels = [label for _key, label in NamingOptions.SCHEMES]
        current = app.naming.scheme
        index = self._scheme_keys.index(current) if current in self._scheme_keys else 0
        self.scheme_var = tk.StringVar(value=self._scheme_labels[index])
        combo = ttk.Combobox(
            body, textvariable=self.scheme_var, values=self._scheme_labels,
            state="readonly", width=30,
        )
        combo.grid(row=2, column=1, sticky="w", padx=(10, 8), pady=(12, 0))
        combo.bind("<<ComboboxSelected>>", lambda e: self._update_preview())

        self.preview = ttk.Label(body, text="", foreground="#8a8a8a", wraplength=420)
        self.preview.grid(row=3, column=1, columnspan=2, sticky="w", padx=(10, 0), pady=(4, 0))

        self.progress = ttk.Progressbar(body, mode="determinate")
        self.progress.grid(row=4, column=0, columnspan=3, sticky="ew", pady=(16, 0))
        self.progress.grid_remove()

        self.status = ttk.Label(body, text="")
        self.status.grid(row=5, column=0, columnspan=3, sticky="w", pady=(6, 0))

        btns = ttk.Frame(body)
        btns.grid(row=6, column=0, columnspan=3, sticky="e", pady=(16, 0))
        self.cancel_btn = ttk.Button(btns, text="取消", command=self.destroy)
        self.cancel_btn.pack(side="left", padx=(0, 8))
        self.start_btn = ttk.Button(btns, text="开始导出", width=14, command=self._start)
        self.start_btn.pack(side="left")

        self._update_preview()
        self.update_idletasks()
        self.geometry("+%d+%d" % (
            app.root.winfo_rootx() + 150, app.root.winfo_rooty() + 120
        ))

    # -- ui helpers --------------------------------------------------------
    def _selected_scheme(self):
        label = self.scheme_var.get()
        try:
            return self._scheme_keys[self._scheme_labels.index(label)]
        except ValueError:
            return "year_author_title"

    def _update_preview(self):
        scheme = self._selected_scheme()
        example = self.items[0].reference
        naming = NamingOptions(scheme=scheme)
        try:
            from exporter import build_base_name

            stem = build_base_name(example, naming, 60)
        except Exception:
            stem = ""
        if scheme == "original":
            self.preview.configure(text="例如：" + (example.pdfs[0].basename if example.pdfs else "原文件名.pdf"))
        else:
            self.preview.configure(text="例如：" + (stem or "？") + ".pdf")

    def _browse(self):
        folder = filedialog.askdirectory(
            parent=self, title="选择导出文件夹", initialdir=self.out_var.get() or os.path.expanduser("~")
        )
        if folder:
            self.out_var.set(folder)

    def _open_dir(self):
        path = self.out_var.get()
        if path and os.path.isdir(path):
            os.startfile(path)

    # -- export ------------------------------------------------------------
    def _start(self):
        if self._running:
            return
        out_dir = self.out_var.get().strip()
        if not out_dir:
            messagebox.showwarning("缺少文件夹", "请先选择导出文件夹。", parent=self)
            return
        naming = NamingOptions(
            scheme=self._selected_scheme(),
        )
        self.app.naming = naming
        options = ExportOptions(out_dir=out_dir, naming=naming, write_manifest=False)
        self._running = True
        self.start_btn.configure(state="disabled")
        self.cancel_btn.configure(state="disabled", text="导出中…")
        self.progress.grid()
        self.progress.configure(maximum=1, value=0)
        self.status.configure(text="正在导出…")
        self.update_idletasks()

        def worker():
            try:
                result = export_items(
                    self.items, options,
                    progress=lambda done, total, label: self._queue.put(
                        ("progress", done, total)
                    ),
                )
                self._queue.put(("done", result))
            except Exception as exc:  # pragma: no cover - defensive
                self._queue.put(("error", exc))

        threading.Thread(target=worker, daemon=True).start()
        self._poll()

    def _poll(self):
        if not self.winfo_exists():
            return
        try:
            while True:
                message = self._queue.get_nowait()
                if message[0] == "progress":
                    _, done, total = message
                    if int(self.progress.cget("maximum")) != total:
                        self.progress.configure(maximum=max(1, total))
                    self.progress.configure(value=min(done, total))
                elif message[0] in ("done", "error"):
                    self._finish(message[1])
                    return
        except queue.Empty:
            pass
        if self._running:
            self.after(80, self._poll)

    def _finish(self, result):
        if not self.winfo_exists():
            return
        self._running = False
        self.progress.configure(value=self.progress.cget("maximum"))
        self.cancel_btn.configure(text="关闭", state="normal")
        self.start_btn.configure(text="完成", command=self.destroy)
        self.start_btn.configure(state="normal")
        if isinstance(result, Exception):
            self.status.configure(text="导出失败：%s" % result)
            messagebox.showerror("导出失败", str(result), parent=self)
            return
        self.result = result
        self.app.on_export_finished(result)
        lines = []
        if result.count:
            lines.append(
                "完成：导出 %d 个 PDF（%s）\n%s"
                % (result.count, human_size(result.total_bytes), result.out_dir)
            )
            self.cancel_btn.pack_forget()
            self.start_btn.configure(text="完成", command=self.destroy)
            ttk.Button(
                self.cancel_btn.master, text="打开文件夹", command=self._open_dir
            ).pack(side="left", padx=(0, 8))
        else:
            lines.append("没有导出任何 PDF。")
        if result.no_pdf:
            lines.append("跳过 %d 篇没有 PDF 的文献。" % len(result.no_pdf))
            for ref in result.no_pdf[:5]:
                lines.append("  · %s %s" % (ref.year, ref.display_title[:40]))
            if len(result.no_pdf) > 5:
                lines.append("  · …另有 %d 篇" % (len(result.no_pdf) - 5))
        if result.missing:
            lines.append("%d 个附件在库中找不到文件，已跳过。" % len(result.missing))
        if result.errors:
            lines.append("%d 个文件复制失败。" % len(result.errors))
        self.status.configure(text="\n".join(lines))
        self.update_idletasks()
        self.geometry("")


# --------------------------------------------------------------------------
# main window
# --------------------------------------------------------------------------

class App:

    def __init__(self, root):
        self.root = root
        self.config = load_config()

        self.libs = {}
        self.lib_membership = {}
        self.sel = {}
        self.node_meta = {}
        self.row_meta = {}
        self.view_refs = []
        self.view_items = []
        self.current_path = None
        self.current_node = None
        self._loading_rows = False
        self._sort_reverse = True
        self._filter_job = None

        self.out_dir = tk.StringVar(value=self.config.get("out_dir", ""))
        self.naming = NamingOptions(
            scheme=self.config.get("naming", "year_author_title"),
        )
        self.search_var = tk.StringVar()
        self.search_var.trace_add("write", lambda *a: self._schedule_filter())

        self._build_fonts()
        self._build_styles()
        self._build_ui()
        self._restore_libraries()
        self._update_status()

    # -- fonts / styles ----------------------------------------------------
    def _build_fonts(self):
        base = 10
        self.font = tkfont.Font(family="Microsoft YaHei UI", size=base)
        self.font_bold = tkfont.Font(family="Microsoft YaHei UI", size=base, weight="bold")
        self.font_small = tkfont.Font(family="Microsoft YaHei UI", size=base - 1)
        for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont", "TkCaptionFont"):
            try:
                tkfont.nametofont(name).configure(family="Microsoft YaHei UI", size=base)
            except tk.TclError:
                pass
        self.row_height = int(self.font.metrics("linespace") * 1.6)

    def _build_styles(self):
        style = ttk.Style(self.root)
        try:
            style.theme_use("vista")
        except tk.TclError:
            pass
        style.configure("Treeview", rowheight=self.row_height, font=self.font)
        style.configure("Treeview.Heading", font=self.font)
        style.configure("Export.TButton", font=self.font_bold)
        style.configure("Muted.TLabel", foreground="#7a7a7a")
        self.root.option_add("*TCombobox*Listbox.font", self.font)

    # -- layout ------------------------------------------------------------
    def _build_ui(self):
        self.root.title(APP_TITLE)
        self.root.geometry(self.config.get("geometry", "1150x720"))
        self.root.minsize(900, 560)

        outer = ttk.Frame(self.root, padding=(10, 8, 10, 8))
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(1, weight=1)

        # one toolbar, four controls
        bar = ttk.Frame(outer)
        bar.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        ttk.Button(bar, text="＋ 添加文献库", command=self.add_library_dialog).pack(side="left")
        ttk.Button(bar, text="重新加载", command=self.reload_libraries).pack(side="left", padx=(6, 0))
        self.export_btn = ttk.Button(
            bar, text="导出选中 PDF…", style="Export.TButton", command=self.open_export_dialog
        )
        self.export_btn.pack(side="left", padx=(18, 0))
        ttk.Label(bar, text="搜索").pack(side="right", padx=(8, 2))
        search = ttk.Entry(bar, textvariable=self.search_var, width=26)
        search.pack(side="right")
        self.search_entry = search

        body = ttk.Panedwindow(outer, orient="horizontal")
        body.grid(row=1, column=0, sticky="nsew")

        left = ttk.Frame(body)
        left.columnconfigure(0, weight=1)
        left.rowconfigure(0, weight=1)
        self.tree = ttk.Treeview(left, show="tree", selectmode="browse")
        self.tree.grid(row=0, column=0, sticky="nsew")
        tree_scroll = ttk.Scrollbar(left, orient="vertical", command=self.tree.yview)
        tree_scroll.grid(row=0, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=tree_scroll.set)
        self.tree.column("#0", width=300, minwidth=200)
        self.tree.tag_configure("lib", font=self.font_bold)
        self.tree.bind("<<TreeviewSelect>>", self._on_tree_select)
        self.tree.bind("<<TreeviewOpen>>", lambda e: None)
        body.add(left, weight=0)

        right = ttk.Frame(body)
        right.columnconfigure(0, weight=1)
        right.rowconfigure(1, weight=1)
        head = ttk.Frame(right)
        head.grid(row=0, column=0, sticky="ew", pady=(0, 4))
        self.view_title = ttk.Label(head, text="", font=self.font_bold)
        self.view_title.pack(side="left")
        self.visible_count = ttk.Label(head, text="", style="Muted.TLabel")
        self.visible_count.pack(side="left", padx=(10, 0))

        table_frame = ttk.Frame(right)
        table_frame.grid(row=1, column=0, sticky="nsew")
        table_frame.columnconfigure(0, weight=1)
        table_frame.rowconfigure(0, weight=1)
        columns = ("year", "authors", "title", "pdf")
        self.table = ttk.Treeview(
            table_frame, columns=columns, show="headings", selectmode="extended"
        )
        self.table.grid(row=0, column=0, sticky="nsew")
        vsb = ttk.Scrollbar(table_frame, orient="vertical", command=self.table.yview)
        vsb.grid(row=0, column=1, sticky="ns")
        hsb = ttk.Scrollbar(table_frame, orient="horizontal", command=self.table.xview)
        hsb.grid(row=1, column=0, sticky="ew")
        self.table.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        specs = {
            "year": ("年份", 64, "center"),
            "authors": ("作者", 150, "w"),
            "title": ("标题", 560, "w"),
            "pdf": ("PDF", 56, "center"),
        }
        for key, (label, width, anchor) in specs.items():
            self.table.heading(key, text=label, command=lambda k=key: self._sort_by(k))
            self.table.column(key, width=width, anchor=anchor, stretch=(key == "title"))
        self.table.tag_configure("nopdf", foreground="#9a9a9a")
        self.table.tag_configure("trash", foreground="#b04a4a")
        self.table.bind("<<TreeviewSelect>>", self._on_table_select)
        self.table.bind("<Control-a>", self._select_all_event)
        body.add(right, weight=1)

        # bottom: hint/status on the left, nothing else
        bottom = ttk.Frame(outer)
        bottom.grid(row=2, column=0, sticky="ew", pady=(8, 0))
        bottom.columnconfigure(0, weight=1)
        self.status = ttk.Label(bottom, text="", anchor="w", style="Muted.TLabel")
        self.status.grid(row=0, column=0, sticky="w")

        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.tree.bind("<Double-1>", self._toggle_tree)

    # -- library handling ---------------------------------------------------
    def _restore_libraries(self):
        for path in self.config.get("libraries", []):
            if os.path.exists(path):
                try:
                    self.add_library_path(path, select=False)
                except LibraryError:
                    continue
        if self.libs:
            self._select_library_node(next(iter(self.libs)))
        else:
            self._show_hint()

    def add_library_dialog(self):
        paths = filedialog.askopenfilenames(
            parent=self.root,
            title="选择 EndNote 文献库",
            filetypes=LIB_FILETYPES,
            initialdir=self.config.get("last_lib_dir") or os.path.expanduser("~"),
        )
        if not paths:
            return
        self.config["last_lib_dir"] = os.path.dirname(paths[0])
        added = []
        for path in paths:
            try:
                lib = self.add_library_path(path, select=False)
                if lib is not None:
                    added.append(lib)
            except LibraryError as exc:
                messagebox.showerror("无法打开文献库", str(exc), parent=self.root)
        if added:
            self._select_library_node(added[0].path)
        self._save_config()

    def add_library_path(self, path, select=True):
        path = os.path.abspath(path)
        if path in self.libs:
            if select:
                self._select_library_node(path)
            return self.libs[path]
        lib = EndNoteLibrary(path)
        self.libs[path] = lib
        self.sel[path] = set()
        membership = {}
        for group in lib.custom_groups:
            for ref_id in group.member_ids:
                membership.setdefault(ref_id, []).append(group.name)
        self.lib_membership[path] = membership
        self._populate_tree()
        if select:
            self._select_library_node(path)
        return lib

    def remove_current_library(self):
        path = self.current_path
        if not path:
            return
        if not messagebox.askyesno(
            "移除文献库",
            "只从本程序移除「%s」，不会删除磁盘上的库文件。继续吗？"
            % os.path.basename(path),
            parent=self.root,
        ):
            return
        lib = self.libs.pop(path, None)
        if lib is not None:
            lib.close()
        self.sel.pop(path, None)
        self.lib_membership.pop(path, None)
        self.current_path = None
        self.current_node = None
        self.view_refs = []
        self.view_items = []
        self._render_table([])
        self._populate_tree()
        if self.libs:
            self._select_library_node(next(iter(self.libs)))
        else:
            self._show_hint()
        self._save_config()

    def reload_libraries(self):
        """Re-read every opened library from disk (new synced records, groups).

        Closing and reopening the EndNoteLibrary re-opens ``sdb.eni``, so both
        references and group membership come from the current database.  The
        current view and the per-library selection are kept where possible.
        """
        paths = list(self.libs.keys())
        current_meta = self.node_meta.get(self.current_node) if self.current_node else None
        saved_selection = {path: set(ids) for path, ids in self.sel.items()}
        for path in paths:
            self.libs[path].close()
        self.libs.clear()
        self.lib_membership.clear()
        self.sel.clear()
        self.current_path = None
        self.current_node = None
        failed = 0
        for path in paths:
            try:
                self.add_library_path(path, select=False)
            except LibraryError as exc:
                failed += 1
                messagebox.showerror("重新加载失败", "%s\n%s" % (path, exc), parent=self.root)
        # keep selections of records that still exist
        for path, ids in saved_selection.items():
            lib = self.libs.get(path)
            if lib is not None:
                self.sel[path] = {i for i in ids if i in lib.by_id}
        if self.libs:
            restored = False
            if current_meta is not None and current_meta[0] in self.libs:
                iid = self._iid_for(*current_meta)
                if iid is not None:
                    self.tree.selection_set(iid)
                    self.tree.see(iid)
                    restored = True
            if not restored:
                self._select_library_node(next(iter(self.libs)))
            if failed == 0:
                stats = self.libs[next(iter(self.libs))].stats()
                self.status.configure(
                    text="已重新加载：%s 共 %d 条文献、%d 个分组（含最新同步的记录）"
                    % (os.path.basename(next(iter(self.libs))), stats["active"], stats["groups"])
                )
        else:
            self._show_hint()

    # -- tree ---------------------------------------------------------------
    def _populate_tree(self):
        old = self.node_meta.get(self.current_node) if self.current_node else None
        self.tree.delete(*self.tree.get_children(""))
        self.node_meta.clear()
        for path, lib in self.libs.items():
            stats = lib.stats()
            label = "%s（%d）" % (os.path.basename(path), stats["active"])
            node = self.tree.insert("", "end", text=label, open=True, tags=("lib",))
            self.node_meta[node] = (path, "lib", None)
            for kind, text, count in (
                ("all", "全部文献", stats["active"]),
                ("unfiled", "未归档", len(lib.unfiled_references())),
            ):
                iid = self.tree.insert(node, "end", text="%s（%d）" % (text, count))
                self.node_meta[iid] = (path, kind, None)
            if lib.custom_groups:
                set_iid = self.tree.insert(node, "end", text="我的组（%d）" % len(lib.custom_groups))
                self.node_meta[set_iid] = (path, "groupset", None)
                for group in lib.custom_groups:
                    gid = self.tree.insert(
                        set_iid, "end",
                        text="%s（%d）" % (group.name, len(group.member_ids)),
                    )
                    self.node_meta[gid] = (path, "group", group.id)
        if old is not None and old[0] in self.libs:
            for iid, meta in self.node_meta.items():
                if meta == old:
                    self.tree.selection_set(iid)
                    self.tree.see(iid)
                    break

    def _select_library_node(self, path):
        for iid, meta in self.node_meta.items():
            if meta[0] == path and meta[1] == "all":
                self.tree.selection_set(iid)
                self.tree.see(iid)
                return

    def _toggle_tree(self, event):
        iid = self.tree.identify_row(event.y)
        if not iid:
            return
        self.tree.item(iid, open=not self.tree.item(iid, "open"))

    def _on_tree_select(self, event=None):
        selection = self.tree.selection()
        if not selection:
            return
        meta = self.node_meta.get(selection[0])
        if not meta:
            return
        path, kind, gid = meta
        if kind == "groupset":
            self.tree.item(selection[0], open=True)
            return
        if kind == "lib":
            kind = "all"
        self.show_view(path, kind, gid)

    # -- views --------------------------------------------------------------
    def show_view(self, path, kind, gid):
        lib = self.libs.get(path)
        if lib is None:
            return
        self.current_path = path
        self.current_node = self._iid_for(path, kind, gid)
        if kind == "all":
            refs, title = lib.active_references, "全部文献"
        elif kind == "unfiled":
            refs, title = lib.unfiled_references(), "未归档"
        elif kind == "group":
            group = lib.groups_by_id.get(gid)
            refs = lib.refs_in_group(gid)
            title = group.name if group else "?"
        else:
            return
        self.view_refs = list(refs)
        self.view_title.configure(text="%s · %s" % (os.path.basename(path), title))
        self._apply_filter()

    def _iid_for(self, path, kind, gid):
        for iid, meta in self.node_meta.items():
            if meta == (path, kind, gid):
                return iid
        return None

    def _schedule_filter(self):
        if self._filter_job:
            self.root.after_cancel(self._filter_job)
        self._filter_job = self.root.after(200, self._apply_filter)

    def _apply_filter(self):
        self._filter_job = None
        refs = self.view_refs
        term = self.search_var.get().strip()
        if term:
            refs = EndNoteLibrary.search(term, refs)
        items = [(self.current_path, r) for r in refs]
        self.view_items = items
        self._render_table(items)

    def _sort_by(self, column):
        if column == "year":
            self._sort_reverse = not self._sort_reverse
        self._apply_filter()

    def _sorted_items(self, items):
        reverse = self._sort_reverse
        try:
            return sorted(
                items,
                key=lambda it: (it[1].year or "0000", it[1].author_label, it[1].title),
                reverse=reverse,
            )
        except TypeError:
            return list(items)

    def _render_table(self, items):
        self._loading_rows = True
        try:
            self.table.delete(*self.table.get_children(""))
            self.row_meta.clear()
            for index, (path, ref) in enumerate(self._sorted_items(items)):
                pdfs = ref.pdfs
                tags = []
                if not pdfs:
                    tags.append("nopdf")
                if ref.trash:
                    tags.append("trash")
                iid = "r%d" % index
                self.table.insert(
                    "", "end", iid=iid,
                    values=(ref.year, ref.author_label, ref.display_title, "%d" % len(pdfs) if pdfs else "—"),
                    tags=tuple(tags),
                )
                self.row_meta[iid] = (path, ref)
        finally:
            self._loading_rows = False
        if self.current_path:
            selected = self.sel.get(self.current_path, set())
            restore = [iid for iid, (path, ref) in self.row_meta.items() if ref.id in selected]
            if restore:
                self.table.selection_set(restore)
                self.table.see(restore[0])
        self.visible_count.configure(
            text=("共 %d 条" % len(items)) if items else "请添加文献库或选择分组"
        )
        self._update_status()

    def _show_hint(self):
        self.view_title.configure(text="使用步骤")
        self.visible_count.configure(text="")
        self.status.configure(text=HINT)

    # -- selection ----------------------------------------------------------
    def _on_table_select(self, event=None):
        if self._loading_rows or not self.current_path:
            return
        store = self.sel.setdefault(self.current_path, set())
        for iid, (path, ref) in self.row_meta.items():
            store.discard(ref.id)
        for iid in self.table.selection():
            ref = self.row_meta.get(iid, (None, None))[1]
            if ref is not None:
                store.add(ref.id)
        self._update_status()

    def select_all_visible(self):
        if not self.row_meta:
            return
        self.table.selection_set(list(self.row_meta.keys()))
        self._on_table_select()

    def _select_all_event(self, event):
        self.select_all_visible()
        return "break"

    def clear_selection(self):
        self.table.selection_remove(*self.table.selection())
        self.sel = {path: set() for path in self.sel}
        self._update_status()

    def _update_status(self):
        path = self.current_path
        cur = len(self.sel.get(path, set())) if path else 0
        total = sum(len(v) for v in self.sel.values())
        if total:
            text = "已选 %d 篇" % total
            if len(self.libs) > 1 and total != cur:
                text += "（含其他文献库中选中的 %d 篇）" % (total - cur)
            text += "　·　点「导出选中 PDF…」把它们复制到一个文件夹"
        else:
            text = "在列表里按住 Ctrl / Shift 点选文献（或 Ctrl+A 全选），然后点「导出选中 PDF…」"
        self.status.configure(text=text)

    # -- export -------------------------------------------------------------
    def collect_items(self):
        items = []
        no_pdf = 0
        for path, ids in self.sel.items():
            lib = self.libs.get(path)
            if not lib or not ids:
                continue
            membership = self.lib_membership.get(path, {})
            for ref_id in sorted(ids):
                ref = lib.by_id.get(ref_id)
                if ref is None:
                    continue
                if not ref.pdfs:
                    no_pdf += 1
                items.append(ExportItem(
                    reference=ref,
                    library=lib.library_name,
                    groups=tuple(membership.get(ref_id, ())),
                ))
        items.sort(key=lambda it: (it.reference.year or "", it.reference.author_label))
        return items, no_pdf

    def open_export_dialog(self):
        if not self.libs:
            messagebox.showinfo("还没有文献库", "请先点「＋ 添加文献库」选择 EndNote 的 .enl 文件。", parent=self.root)
            return
        items, no_pdf = self.collect_items()
        if not items:
            messagebox.showinfo(
                "还没有选择文献",
                "请先在右边的列表里点选一篇或多篇文献：\n"
                "按住 Ctrl 或 Shift 可多选，Ctrl+A 可全选。",
                parent=self.root,
            )
            return
        dialog = ExportDialog(self, items, no_pdf)
        dialog.grab_set()
        self.root.wait_window(dialog)
        if dialog.result is not None:
            self.out_dir.set(dialog.out_var.get())
            self._save_config()

    def on_export_finished(self, result):
        pass

    # -- misc ---------------------------------------------------------------
    def show_help(self):
        messagebox.showinfo("使用说明", HINT, parent=self.root)

    def show_about(self):
        messagebox.showinfo(
            "关于",
            "%s\n\n从 EndNote 文献库导出选中文献的 PDF，供导入腾讯 ima 知识库。\n"
            "只读取 EndNote 库并把 PDF 复制到文件夹，"
            "不修改、不移动、不删除你的文献库。" % APP_TITLE,
            parent=self.root,
        )

    def _save_config(self):
        data = {
            "libraries": list(self.libs.keys()),
            "out_dir": self.out_dir.get(),
            "naming": self.naming.scheme,
            "geometry": self.root.winfo_geometry(),
            "last_lib_dir": self.config.get("last_lib_dir", ""),
        }
        self.config.update(data)
        save_config(data)

    def on_close(self):
        try:
            self._save_config()
        finally:
            for lib in self.libs.values():
                try:
                    lib.close()
                except Exception:
                    pass
            self.root.destroy()


# --------------------------------------------------------------------------
# entry point
# --------------------------------------------------------------------------

def _run_selftest(argv):
    """Headless end-to-end check used to verify the packaged .exe.

    ``endnote-pdf-exporter.exe --selftest --library <enl> --group <name> --out <dir>
    --report <json>`` exports that group without opening a window and writes a
    small JSON summary.  Normal users never need this.
    """
    import json as _json

    # a windowed exe may have no console, or a GBK console; never crash on output
    try:
        if sys.stdout is not None:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    def opt(name, default=""):
        if name in argv:
            index = argv.index(name)
            if index + 1 < len(argv):
                return argv[index + 1]
        return default

    library_path = opt("--library")
    out_dir = opt("--out")
    report_path = opt("--report")
    group_name = opt("--group")
    if not library_path or not out_dir:
        return 2
    try:
        lib = EndNoteLibrary(library_path)
    except LibraryError as exc:
        if report_path:
            with open(report_path, "w", encoding="utf-8") as fh:
                _json.dump({"ok": False, "error": str(exc)}, fh, ensure_ascii=False)
        return 2
    if group_name:
        group = next((g for g in lib.custom_groups if g.name == group_name), None)
        refs = lib.refs_in_group(group.id) if group else []
    else:
        refs = [r for r in lib.active_references if r.has_pdf][:10]
    membership = {}
    for group in lib.custom_groups:
        for ref_id in group.member_ids:
            membership.setdefault(ref_id, []).append(group.name)
    items = [
        ExportItem(
            reference=ref,
            library=lib.library_name,
            groups=tuple(membership.get(ref.id, ())),
        )
        for ref in refs
    ]
    result = export_items(
        items,
        ExportOptions(out_dir=out_dir, naming=NamingOptions(scheme="year_author_title")),
    )
    lib.close()
    summary = {
        "ok": bool(result.count),
        "python": sys.version.split()[0],
        "frozen": bool(getattr(sys, "frozen", False)),
        "library": os.path.basename(library_path),
        "group": group_name,
        "stats": lib.stats(),
        "selected_refs": len(items),
        "exported": result.count,
        "bytes": result.total_bytes,
        "no_pdf": len(result.no_pdf),
        "missing": len(result.missing),
        "errors": [str(e[1]) for e in result.errors],
        "out_dir": result.out_dir,
        "files": [f.name for f in result.files[:5]],
    }
    if report_path:
        with open(report_path, "w", encoding="utf-8") as fh:
            _json.dump(summary, fh, ensure_ascii=False, indent=2)
    try:
        print(_json.dumps(summary, ensure_ascii=False, indent=2))
    except Exception:
        pass
    return 0 if summary["ok"] else 1


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--selftest" in argv:
        return _run_selftest(argv)
    enable_dpi_awareness()
    root = tk.Tk()
    try:
        root.tk.call("tk", "scaling", root.winfo_fpixels("1i") / 72.0)
    except tk.TclError:
        pass
    app = App(root)
    index = 0
    while index < len(argv):
        arg = argv[index]
        if arg in ("--library", "-l") and index + 1 < len(argv):
            try:
                app.add_library_path(argv[index + 1])
            except LibraryError as exc:
                messagebox.showerror("无法打开文献库", str(exc), parent=root)
            index += 2
            continue
        index += 1
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
