# -*- coding: utf-8 -*-
"""Copy the PDF attachments of selected EndNote references into one folder.

The folder is what you import into the 腾讯 ima knowledge base; nothing else
is needed.  A small CSV manifest (optional) records where every file came from.
"""

from __future__ import annotations

import csv
import os
import re
import time
from dataclasses import dataclass, field

from endnote_lib import sanitize_filename

__all__ = [
    "ExportItem",
    "NamingOptions",
    "ExportOptions",
    "ExportResult",
    "export_items",
    "suggest_out_dir",
]

MAX_PATH = 250  # keep a little margin below the Windows 260 limit


# --------------------------------------------------------------------------
# naming
# --------------------------------------------------------------------------

@dataclass
class NamingOptions:
    """How exported PDF files are named."""

    SCHEMES = (
        ("year_author_title", "年份-作者-标题（推荐）"),
        ("author_year_title", "作者-年份-标题"),
        ("title", "只用标题"),
        ("original", "保留原 PDF 文件名"),
        ("custom", "自定义模板"),
    )

    scheme: str = "year_author_title"
    template: str = "{year}-{author}-{title}"
    max_len: int = 110

    def label(self):
        for key, text in self.SCHEMES:
            if key == self.scheme:
                return text
        return self.scheme


def _safe(value, max_len):
    return sanitize_filename(value, max_len=max_len)


def _join_parts(parts, sep="-", max_len=110):
    parts = [_safe(p, max_len) for p in parts if p and str(p).strip()]
    text = sep.join(parts)
    text = re.sub(r"\s*-\s*", "-", text)
    text = text.strip(" -.")
    if len(text) > max_len:
        text = text[:max_len].rstrip(" -.")
    return text or "未命名文献"


def build_base_name(ref, naming, max_len):
    """Return the file name stem for *ref* according to *naming*."""
    scheme = naming.scheme
    year = ref.year
    first = ref.first_author or ref.author_label
    title = ref.display_title
    if scheme == "original":
        return None  # handled per-attachment
    if scheme == "year_author_title":
        return _join_parts([year, first, title], max_len=max_len)
    if scheme == "author_year_title":
        return _join_parts([first, year, title], max_len=max_len)
    if scheme == "title":
        return _join_parts([title], max_len=max_len)
    # custom template
    try:
        text = naming.template.format(
            year=year,
            author=first,
            author_full=ref.author_label,
            title=title,
            journal=ref.journal,
            id=ref.id,
            type=ref.type_label,
            library=getattr(ref, "library_name", ""),
        )
    except (KeyError, IndexError, ValueError) as exc:
        raise ValueError("自定义模板无法解析：%s（可用占位符：{year} {author} {author_full} {title} {journal} {id} {type} {library}）" % exc)
    return _join_parts([text], max_len=max_len)


# --------------------------------------------------------------------------
# export options / result
# --------------------------------------------------------------------------

@dataclass
class ExportOptions:
    out_dir: str
    naming: NamingOptions = field(default_factory=NamingOptions)
    write_manifest: bool = True
    manifest_name: str = "EndNote导出清单.csv"
    subfolder_by_library: bool = False
    overwrite: bool = False
    copy_non_pdf: bool = False


@dataclass
class ExportItem:
    reference: object
    library: str = ""
    groups: tuple = ()

    @property
    def key(self):
        return (self.library, self.reference.id)


@dataclass
class ExportedFile:
    name: str
    path: str
    size: int
    ref_id: int
    library: str
    title: str
    authors: str
    year: str
    journal: str
    doi: str
    type_label: str
    groups: str
    source: str


@dataclass
class ExportResult:
    out_dir: str = ""
    files: list = field(default_factory=list)
    missing: list = field(default_factory=list)      # (ref, attachment)
    errors: list = field(default_factory=list)       # (ref, message)
    no_pdf: list = field(default_factory=list)       # refs without any PDF
    manifest_path: str = ""
    seconds: float = 0.0

    @property
    def total_bytes(self):
        return sum(f.size for f in self.files)

    @property
    def count(self):
        return len(self.files)


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def suggest_out_dir(base=None):
    """A friendly default output folder (not created here)."""
    if base is None:
        base = os.path.join(os.path.expanduser("~"), "Documents")
    stamp = time.strftime("%Y%m%d_%H%M")
    return os.path.join(base, "EndNote导出PDF_%s" % stamp)


def _unique_name(stem, used, suffix=".pdf"):
    """Return a case-insensitively unique ``stem + suffix`` name."""
    candidate = stem + suffix
    counter = 2
    while candidate.lower() in used:
        candidate = "%s_%d%s" % (stem, counter, suffix)
        counter += 1
    used.add(candidate.lower())
    return candidate


def _fit_stem(stem, out_dir, suffix=".pdf"):
    """Trim *stem* so that out_dir/stem+suffix stays inside MAX_PATH."""
    room = MAX_PATH - len(out_dir) - len(suffix) - 1
    if room < 40:
        room = 40
    if len(stem) > room:
        stem = stem[:room].rstrip(" .-_")
    return stem


def _bytes_label(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return ("%.1f %s" % (n, unit)) if unit != "B" else ("%d B" % n)
        n /= 1024.0


def _manifest_rows(result):
    rows = []
    for i, f in enumerate(result.files, 1):
        rows.append([
            i, f.name, f.year, f.authors, f.title, f.journal, f.doi,
            f.type_label, f.ref_id, f.library, f.groups, f.source,
            "%.1f" % (f.size / 1024.0),
        ])
    return rows


MANIFEST_HEADER = [
    "序号", "文件名", "年份", "作者", "标题", "期刊", "DOI",
    "文献类型", "EndNote记录号", "来源库", "EndNote分组", "原始PDF",
    "大小(KB)",
]


# --------------------------------------------------------------------------
# main entry point
# --------------------------------------------------------------------------

def export_items(items, options, progress=None):
    """Copy every PDF of *items* into ``options.out_dir``.

    *progress(done, total, label)* is called after every copied file.
    """
    started = time.time()
    result = ExportResult(out_dir=os.path.abspath(options.out_dir))
    os.makedirs(result.out_dir, exist_ok=True)

    jobs = []          # (item, attachment)
    dedupe = set()     # same attachment reached twice -> copy once
    for item in items:
        ref = item.reference
        pdfs = [a for a in ref.pdfs]
        if not pdfs:
            result.no_pdf.append(ref)
            continue
        for att in pdfs:
            ident = (att.rel_path, item.library)
            if ident in dedupe:
                continue
            dedupe.add(ident)
            jobs.append((item, att))

    total = len(jobs)
    used_names = set()
    for sub in os.listdir(result.out_dir):
        used_names.add(sub.lower())

    done = 0
    max_len = options.naming.max_len
    for item, att in jobs:
        ref = item.reference
        target_dir = result.out_dir
        if options.subfolder_by_library and item.library:
            target_dir = os.path.join(result.out_dir, sanitize_filename(item.library, 80))
            os.makedirs(target_dir, exist_ok=True)
        if not att.exists:
            result.missing.append((ref, att))
            done += 1
            if progress:
                progress(done, total, att.basename)
            continue
        try:
            if options.naming.scheme == "original":
                stem = _safe(os.path.splitext(att.basename)[0], max_len)
            else:
                stem = build_base_name(ref, options.naming, max_len)
            stem = _fit_stem(stem, target_dir)
            # a reference may carry several PDFs: number them
            siblings = [a for a in ref.pdfs if a.exists]
            if len(siblings) > 1 and options.naming.scheme != "original":
                index = siblings.index(att) + 1
                if index > 1:
                    stem = _fit_stem("%s_%d" % (stem, index), target_dir)
            name = _unique_name(stem, used_names)
            dst = os.path.join(target_dir, name)
            if os.path.exists(dst) and options.overwrite:
                os.remove(dst)
            att.copy_to(dst)
            size = os.path.getsize(dst)
            result.files.append(ExportedFile(
                name=name,
                path=dst,
                size=size,
                ref_id=ref.id,
                library=item.library or getattr(ref, "library_name", ""),
                title=ref.display_title,
                authors=ref.author_label,
                year=ref.year,
                journal=ref.journal,
                doi=ref.doi,
                type_label=ref.type_label,
                groups="; ".join(item.groups),
                source=att.rel_path,
            ))
        except (OSError, FileExistsError) as exc:
            result.errors.append((ref, "%s: %s" % (att.basename, exc)))
        done += 1
        if progress:
            progress(done, total, att.basename)

    if options.write_manifest and result.files:
        manifest = os.path.join(result.out_dir, options.manifest_name)
        if os.path.exists(manifest):
            stamp = time.strftime("%Y%m%d_%H%M%S")
            root, ext = os.path.splitext(options.manifest_name)
            manifest = os.path.join(result.out_dir, "%s_%s%s" % (root, stamp, ext))
        try:
            with open(manifest, "w", encoding="utf-8-sig", newline="") as fh:
                writer = csv.writer(fh)
                writer.writerow(MANIFEST_HEADER)
                writer.writerows(_manifest_rows(result))
            result.manifest_path = manifest
        except OSError as exc:
            result.errors.append((None, "写入清单失败：%s" % exc))

    result.seconds = time.time() - started
    return result
