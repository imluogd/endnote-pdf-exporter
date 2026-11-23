# -*- coding: utf-8 -*-
"""Read-only access to EndNote desktop libraries (EndNote 20/21, Windows).

An EndNote library on disk is a pair::

    My Library.enl            <- main database (locked while EndNote is open)
    My Library.Data/
        sdb/sdb.eni           <- SQLite: refs, file_res (attachments), groups, ...
        sdb/pdb.eni           <- SQLite full-text PDF index (not used here)
        PDF/<random>/<file>   <- the attached files themselves

A compressed library (``My Library.enlx``) is a ZIP with the same tree; this
module reads it without unpacking everything to disk.

The database is opened read-only (``mode=ro``) and nothing here ever writes to
the library, so EndNote may keep running while the export tool works.
"""

from __future__ import annotations

import html
import os
import re
import shutil
import sqlite3
import tempfile
import unicodedata
import zipfile

__all__ = [
    "EndNoteLibrary",
    "Reference",
    "Attachment",
    "Group",
    "LibraryError",
    "sanitize_filename",
    "clean_title",
    "surname_of",
]


class LibraryError(Exception):
    """Raised when a library file cannot be opened."""


# --------------------------------------------------------------------------
# reference types (EndNote 20 numbering, verified against RefTypeTable.xml)
# --------------------------------------------------------------------------

_REF_TYPES = {
    0: "期刊论文",
    1: "图书",
    2: "学位论文",
    3: "会议论文集",
    4: "个人通讯",
    5: "报纸文章",
    6: "软件",
    7: "图书章节",
    8: "杂志文章",
    9: "编著",
    10: "报告",
    11: "地图",
    12: "音像资料",
    13: "艺术作品",
    15: "专利",
    16: "网页",
    17: "法案",
    18: "案例",
    19: "听证会",
    20: "手稿",
    21: "影视资料",
    22: "法规",
    28: "电子文章",
    29: "电子书",
    30: "在线数据库",
    31: "通用",
    32: "政府文件",
    33: "会议论文",
    34: "在线多媒体",
    35: "经典著作",
    37: "未发表",
    42: "聚合数据库",
    43: "博客",
    46: "连续出版物",
    47: "标准",
    48: "数据集",
    49: "电子书章节",
}

# EndNote keeps layout tags like <i>…</i> inside title fields
_LAYOUT_TAGS = re.compile(
    r"</?(?:i|b|u|italic|bold|underline|sup|sub|span|font|br|smallcap|"
    r"superscript|subscript)\b[^>]*>",
    re.IGNORECASE,
)
_HTML_TAGS = re.compile(r"</?[a-zA-Z][^>]{0,40}>")
_PDF_PLACEHOLDER = re.compile(r"^<[^<>]+\.pdf>$", re.IGNORECASE)

_WIN_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *("COM%d" % i for i in range(1, 10)),
    *("LPT%d" % i for i in range(1, 10)),
}
_BAD_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

_COLLATE_RE = re.compile(r"COLLATE\s+([A-Za-z_][A-Za-z0-9_]*)", re.IGNORECASE)
_GROUP_NAME_RE = re.compile(r"<name>(.*?)</name>", re.S)
_GROUP_TYPE_RE = re.compile(r"TYPE;(\d+)")


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------

def sanitize_filename(name, max_len=110):
    """Turn arbitrary text into a Windows-safe file name (without extension)."""
    if not name:
        return "untitled"
    name = unicodedata.normalize("NFC", str(name))
    name = _BAD_CHARS.sub(" ", name)
    name = re.sub(r"\s+", " ", name).strip(" .")
    if len(name) > max_len:
        name = name[:max_len].rstrip(" .")
    if not name:
        name = "untitled"
    if name.split(".")[0].upper() in _WIN_RESERVED:
        name = "_" + name
    return name


def _text(value):
    """Normalise an EndNote TEXT column: CR line breaks become newlines."""
    if value is None:
        return ""
    if isinstance(value, bytes):
        value = value.decode("utf-8", "replace")
    return str(value).replace("\r\n", "\n").replace("\r", "\n").strip()


def _one_line(value):
    return re.sub(r"\s+", " ", _text(value)).strip()


def clean_title(value):
    """Titles may carry layout tags or be a ``<name.pdf>`` placeholder."""
    title = _one_line(value)
    if not title:
        return ""
    if _PDF_PLACEHOLDER.match(title):
        return ""
    title = _LAYOUT_TAGS.sub("", title)
    if "<" in title and ">" in title:
        title = _HTML_TAGS.sub("", title)
    title = html.unescape(title)
    return re.sub(r"\s+", " ", title).strip(" .")


def surname_of(author_entry):
    """'Shannon, R. J.' -> 'Shannon'; 'van der Waals, J.' -> 'van der Waals'."""
    entry = _one_line(author_entry)
    if not entry:
        return ""
    if "," in entry:
        return entry.split(",", 1)[0].strip()
    return entry


def _looks_like_sqlite(path):
    try:
        with open(path, "rb") as fh:
            return fh.read(16) == b"SQLite format 3\x00"
    except OSError:
        return False


def _register_collations(conn):
    """Register EndNote's custom collations (ENCI_Base, ENCIN_zh_CN, ...).

    Plain queries work without this, but any ORDER BY on a collated column
    would fail with "no such collation sequence".
    """
    try:
        rows = conn.execute(
            "SELECT sql FROM sqlite_master WHERE sql IS NOT NULL"
        ).fetchall()
    except sqlite3.Error:
        return
    names = set()
    for row in rows:
        for match in _COLLATE_RE.finditer(row[0] or ""):
            names.add(match.group(1))
    for name in names:
        if name.lower() in ("binary", "nocase", "rtrim"):
            continue
        try:
            conn.create_collation(name, lambda a, b: (a > b) - (a < b))
        except sqlite3.Error:
            pass


# --------------------------------------------------------------------------
# data model
# --------------------------------------------------------------------------

class Attachment:
    """One row of ``file_res``: a file attached to a reference.

    ``root`` is the folder that holds ``rel_path`` for a normal library; for a
    compressed ``.enlx`` library ``archive`` is the open ZipFile and
    ``rel_path`` is the member name inside it.
    """

    __slots__ = ("rel_path", "root", "archive", "exists")

    def __init__(self, rel_path, root=None, archive=None):
        self.rel_path = rel_path.replace("\\", "/")
        self.root = root
        self.archive = archive
        self.exists = False

    @property
    def basename(self):
        return os.path.basename(self.rel_path)

    @property
    def suffix(self):
        return os.path.splitext(self.basename)[1].lower()

    @property
    def is_pdf(self):
        return self.suffix == ".pdf"

    def disk_path(self):
        """Absolute path on disk, or None for a compressed library."""
        if self.root is None:
            return None
        return os.path.join(self.root, self.rel_path.replace("/", os.sep))

    def copy_to(self, dst, overwrite=False):
        """Copy this attachment to *dst*; returns the final path."""
        if not overwrite and os.path.exists(dst):
            raise FileExistsError(dst)
        os.makedirs(os.path.dirname(dst) or ".", exist_ok=True)
        if self.root is not None:
            shutil.copy2(self.disk_path(), dst)
            return dst
        with self.archive.open(self.rel_path) as src, open(dst, "wb") as out:
            shutil.copyfileobj(src, out, 1024 * 1024)
        return dst

    def read_bytes(self):
        if self.root is not None:
            with open(self.disk_path(), "rb") as fh:
                return fh.read()
        with self.archive.open(self.rel_path) as fh:
            return fh.read()

    def __repr__(self):
        return "Attachment(%r)" % (self.rel_path,)


class Reference:
    """One EndNote record, with its attachments pre-loaded."""

    __slots__ = (
        "id", "reference_type", "authors_raw", "year", "title", "journal",
        "volume", "issue", "pages", "doi", "url", "abstract", "keywords",
        "publisher", "place_published", "isbn", "trash", "added_ts",
        "updated_ts", "attachments", "library_name",
    )

    def __init__(self, row):
        self.id = row["id"]
        self.reference_type = row["reference_type"] or 0
        self.authors_raw = _text(row["author"])
        self.year = _one_line(row["year"])
        self.title = clean_title(row["title"])
        self.journal = _one_line(row["secondary_title"])
        self.volume = _one_line(row["volume"])
        self.issue = _one_line(row["number"])
        self.pages = _one_line(row["pages"])
        self.doi = _one_line(row["electronic_resource_number"])
        self.url = _one_line(row["url"])
        self.abstract = _text(row["abstract"])
        self.keywords = _one_line(row["keywords"])
        self.publisher = _one_line(row["publisher"])
        self.place_published = _one_line(row["place_published"])
        self.isbn = _one_line(row["isbn"])
        self.trash = int(row["trash_state"] or 0)
        self.added_ts = int(row["added_to_library"] or 0)
        self.updated_ts = int(row["record_last_updated"] or 0)
        self.attachments = []
        self.library_name = ""

    # -- derived -----------------------------------------------------------
    @property
    def authors(self):
        return [a.strip() for a in self.authors_raw.split("\n") if a.strip()]

    @property
    def first_author(self):
        return surname_of(self.authors[0]) if self.authors else ""

    @property
    def author_label(self):
        names = self.authors
        if not names:
            return "佚名"
        first = surname_of(names[0])
        if len(names) == 1:
            return first
        if len(names) == 2:
            return "%s & %s" % (first, surname_of(names[1]))
        return "%s et al." % first

    @property
    def type_label(self):
        return _REF_TYPES.get(self.reference_type, "类型 %d" % self.reference_type)

    @property
    def pdfs(self):
        return [a for a in self.attachments if a.is_pdf]

    @property
    def other_files(self):
        return [a for a in self.attachments if not a.is_pdf]

    @property
    def has_pdf(self):
        return any(a.exists for a in self.pdfs)

    @property
    def display_title(self):
        if self.title:
            return self.title
        for att in self.pdfs:
            stem = os.path.splitext(att.basename)[0]
            if stem:
                return stem
        return "(无标题)"

    @property
    def search_blob(self):
        return " ".join((
            self.title, self.authors_raw, self.year, self.journal, self.doi,
            self.keywords, self.publisher, self.abstract[:400],
        )).lower()

    def __repr__(self):
        return "Reference(%d, %r)" % (self.id, self.title[:40])


class Group:
    """A custom group (folder) of an EndNote library."""

    __slots__ = ("id", "name", "kind", "member_ids")

    def __init__(self, gid, name, kind, member_ids):
        self.id = gid
        self.name = name
        self.kind = kind
        self.member_ids = list(member_ids)

    @property
    def is_custom(self):
        return self.kind == "custom"

    def __repr__(self):
        return "Group(%d, %r, %s)" % (self.id, self.name, self.kind)


# --------------------------------------------------------------------------
# library
# --------------------------------------------------------------------------

class EndNoteLibrary:
    """Read an EndNote library (``.enl`` or compressed ``.enlx``)."""

    _REF_SQL = """
        SELECT id, trash_state, reference_type, author, year, title,
               secondary_title, volume, number, pages,
               electronic_resource_number, url, abstract, keywords,
               publisher, place_published, isbn,
               added_to_library, record_last_updated
        FROM refs
    """

    def __init__(self, path):
        path = os.path.abspath(os.path.expanduser(path))
        if not os.path.exists(path):
            raise LibraryError("找不到文件：%s" % path)
        self.path = path
        self.folder = os.path.dirname(path)
        self.data_dir = None
        self.sdb_path = None
        self.library_name = os.path.splitext(os.path.basename(path))[0]
        self._zip = None
        self._zip_prefix = ""
        self._temp_dir = None
        self._db_temp_dir = None
        self._names_cache = None
        self._conn = None

        if os.path.isdir(path):
            raise LibraryError("请选择 .enl 或 .enlx 文件，而不是文件夹：%s" % path)
        if path.lower().endswith(".enlx"):
            self._open_enlx(path)
        elif path.lower().endswith(".enl"):
            self._open_enl(path)
        else:
            raise LibraryError("不是 EndNote 库文件：%s" % path)

        self.references = []
        self.by_id = {}
        self.groups = []
        self.groups_by_id = {}
        self.load()

    # -- opening -----------------------------------------------------------
    def _open_enl(self, path):
        data_dir = self._find_data_dir(path)
        if data_dir is None:
            raise LibraryError(
                "找不到与 %s 配套的 .Data 文件夹。\n"
                "请确认库没有被移动或重命名（.enl 与 .Data 必须同名同目录）。"
                % os.path.basename(path)
            )
        self.data_dir = data_dir
        sdb = os.path.join(data_dir, "sdb", "sdb.eni")
        if os.path.exists(sdb):
            self.sdb_path = sdb
        elif _looks_like_sqlite(path):
            # EndNote closed and .Data copy missing: the .enl itself is SQLite
            self.sdb_path = path
        else:
            raise LibraryError("找不到数据库文件：%s" % sdb)

    @staticmethod
    def _find_data_dir(enl_path):
        stem = os.path.splitext(os.path.basename(enl_path))[0]
        folder = os.path.dirname(enl_path)
        exact = os.path.join(folder, stem + ".Data")
        if os.path.isdir(exact):
            return exact
        try:
            for entry in os.listdir(folder):
                full = os.path.join(folder, entry)
                if os.path.isdir(full) and entry.lower() == (stem + ".Data").lower():
                    return full
        except OSError:
            pass
        return None

    def _open_enlx(self, path):
        try:
            zf = zipfile.ZipFile(path)
        except (zipfile.BadZipFile, OSError) as exc:
            raise LibraryError("无法读取压缩库 %s：%s" % (os.path.basename(path), exc))
        sdb_name = None
        for name in zf.namelist():
            low = name.lower()
            if low.endswith("sdb/sdb.eni"):
                sdb_name = name
                break
        if sdb_name is None:
            zf.close()
            raise LibraryError("压缩库中没有找到 sdb/sdb.eni：%s" % os.path.basename(path))
        self._zip = zf
        self._zip_prefix = sdb_name[: -len("sdb/sdb.eni")]
        self._names_cache = set(n.lower() for n in zf.namelist())
        self._temp_dir = tempfile.mkdtemp(prefix="endnote2ima_")
        local_sdb = os.path.join(self._temp_dir, "sdb.eni")
        with zf.open(sdb_name) as src, open(local_sdb, "wb") as out:
            shutil.copyfileobj(src, out, 1024 * 1024)
        self.sdb_path = local_sdb

    # -- sqlite ------------------------------------------------------------
    def _connect(self):
        if self._conn is not None:
            return self._conn
        uri = "file:" + self.sdb_path.replace("\\", "/").replace("#", "%23") + "?mode=ro"
        try:
            conn = sqlite3.connect(uri, uri=True, timeout=4)
            conn.execute("SELECT 1 FROM refs LIMIT 1")
        except sqlite3.Error:
            # EndNote may hold a lock; work on a throw-away copy instead.
            tmp = tempfile.mkdtemp(prefix="endnote2ima_db_")
            copy = os.path.join(tmp, "sdb.eni")
            shutil.copyfile(self.sdb_path, copy)
            conn = sqlite3.connect(
                "file:" + copy.replace("\\", "/") + "?mode=ro&immutable=1",
                uri=True, timeout=4,
            )
            self._db_temp_dir = tmp
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA query_only = ON")
        except sqlite3.Error:
            pass
        _register_collations(conn)
        self._conn = conn
        return conn

    def close(self):
        if self._conn is not None:
            try:
                self._conn.close()
            except sqlite3.Error:
                pass
            self._conn = None
        if self._zip is not None:
            try:
                self._zip.close()
            except OSError:
                pass
            self._zip = None
        for attr in ("_temp_dir", "_db_temp_dir"):
            tmp = getattr(self, attr, None)
            if tmp and os.path.isdir(tmp):
                shutil.rmtree(tmp, ignore_errors=True)
                setattr(self, attr, None)

    # -- loading -----------------------------------------------------------
    def load(self):
        conn = self._connect()
        refs = []
        by_id = {}
        for row in conn.execute(self._REF_SQL):
            ref = Reference(row)
            ref.library_name = self.library_name
            refs.append(ref)
            by_id[ref.id] = ref
        self.references = refs
        self.by_id = by_id
        self._attach_files(conn, by_id)
        self._load_groups(conn)

    def _attach_files(self, conn, by_id):
        pdf_dir = None
        if self.data_dir:
            for entry in ("PDF", "Pdf", "pdf"):
                cand = os.path.join(self.data_dir, entry)
                if os.path.isdir(cand):
                    pdf_dir = cand
                    break
        zip_names = self._names_cache or set()
        zip_root = (self._zip_prefix + "PDF/").lower()
        for row in conn.execute(
            "SELECT refs_id, file_path, file_type, file_pos "
            "FROM file_res ORDER BY refs_id, file_pos"
        ):
            ref = by_id.get(row["refs_id"])
            if ref is None:
                continue
            rel = _text(row["file_path"])
            if not rel:
                continue
            rel = rel.replace("\\", "/")
            if rel.lower().startswith("internal-pdf://"):
                rel = rel[len("internal-pdf://"):]
            if os.path.isabs(rel):
                att = Attachment(os.path.basename(rel), root=os.path.dirname(rel))
                att.exists = os.path.exists(rel)
            elif pdf_dir is not None:
                rel_pdf = rel[len("PDF/"):] if rel.lower().startswith("pdf/") else rel
                att = Attachment(rel_pdf, root=pdf_dir)
                att.exists = os.path.exists(att.disk_path())
            else:
                member = self._zip_prefix + "PDF/" + rel
                att = Attachment(member, archive=self._zip)
                att.exists = member.lower() in zip_names
            ref.attachments.append(att)

    def _load_groups(self, conn):
        groups = []
        for row in conn.execute(
            "SELECT group_id, spec, members FROM groups ORDER BY group_id"
        ):
            name, kind = _parse_group_spec(row["spec"])
            members = _parse_members(row["members"]) if kind == "custom" else []
            groups.append(Group(row["group_id"], name, kind, members))
        self.groups = groups
        self.groups_by_id = {g.id: g for g in groups}

    # -- queries -----------------------------------------------------------
    @property
    def custom_groups(self):
        return [g for g in self.groups if g.is_custom]

    @property
    def active_references(self):
        return [r for r in self.references if r.trash == 0]

    @property
    def trash_references(self):
        return [r for r in self.references if r.trash != 0]

    def refs_in_group(self, group_id):
        group = self.groups_by_id.get(group_id)
        if group is None:
            return []
        return [self.by_id[i] for i in group.member_ids if i in self.by_id]

    def refs_by_ids(self, ids):
        return [self.by_id[i] for i in ids if i in self.by_id]

    def unfiled_references(self):
        filed = set()
        for group in self.custom_groups:
            filed.update(group.member_ids)
        return [r for r in self.active_references if r.id not in filed]

    def recent_references(self, limit=50):
        refs = [r for r in self.active_references if r.added_ts]
        refs.sort(key=lambda r: r.added_ts, reverse=True)
        return refs[:limit]

    @staticmethod
    def search(term, refs):
        """All whitespace-separated tokens must appear in the record."""
        tokens = [t for t in term.lower().split() if t]
        if not tokens:
            return list(refs)
        return [ref for ref in refs if all(tok in ref.search_blob for tok in tokens)]

    def stats(self):
        active = self.active_references
        with_pdf = 0
        pdf_files = 0
        missing = 0
        for ref in active:
            has_one = False
            for att in ref.attachments:
                if not att.is_pdf:
                    continue
                if att.exists:
                    pdf_files += 1
                    has_one = True
                else:
                    missing += 1
            if has_one:
                with_pdf += 1
        return {
            "references": len(self.references),
            "active": len(active),
            "with_pdf": with_pdf,
            "pdf_files": pdf_files,
            "missing_pdf": missing,
            "groups": len(self.custom_groups),
            "trash": len(self.trash_references),
        }

    def __repr__(self):
        return "EndNoteLibrary(%r)" % os.path.basename(self.path)


# --------------------------------------------------------------------------
# group blobs
# --------------------------------------------------------------------------

def _parse_group_spec(blob):
    """Decode ``groups.spec``; its XML is simple enough for regex parsing.

    Example::

        <group version="1"><ids><id>F73342CA-…</id><name>我的组</name></ids>
        <times>…</times><rules><rule>TYPE;3</rule></rules></group>

    ``TYPE;3`` marks a normal custom group, ``TYPE;6`` an online-search group.
    """
    spec = ""
    if blob:
        spec = blob.decode("utf-8", "replace") if isinstance(blob, bytes) else str(blob)
    name = ""
    match = _GROUP_NAME_RE.search(spec)
    if match:
        name = html.unescape(match.group(1)).strip()
    kind_code = None
    match = _GROUP_TYPE_RE.search(spec)
    if match:
        kind_code = int(match.group(1))
    if kind_code == 3:
        kind = "custom"
    elif kind_code == 6:
        kind = "online"
    else:
        kind = "other"
    if not name:
        name = "(未命名组)"
    return name, kind


def _parse_members(blob):
    """``groups.members`` = 4-byte marker, u32 count, count * u32 record ids."""
    if not blob:
        return []
    blob = bytes(blob)
    if len(blob) < 8:
        return []
    if blob[0:4] not in (b"\x00\x00\x00\x02", b"\x02\x00\x00\x00"):
        return []
    count = int.from_bytes(blob[4:8], "little")
    if count < 0 or 8 + count * 4 > len(blob):
        return []
    return [
        int.from_bytes(blob[8 + i * 4: 12 + i * 4], "little") for i in range(count)
    ]
