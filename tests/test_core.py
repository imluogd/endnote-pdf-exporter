# -*- coding: utf-8 -*-
"""Unit tests for the EndNote reader / exporter.

They run against a real EndNote library so they skip themselves when no
library is available.  Point them at one with the ``ENDNOTE_LIBRARY``
environment variable, or open a library once in the GUI (it is remembered).
The tests deliberately avoid hard-coded group names / record ids so they work
with any library.
"""

import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from endnote_lib import (  # noqa: E402
    EndNoteLibrary,
    clean_title,
    sanitize_filename,
    surname_of,
)
from exporter import (  # noqa: E402
    ExportItem,
    ExportOptions,
    NamingOptions,
    build_base_name,
    export_items,
)
from paths import default_library, default_sample_enlx  # noqa: E402

LIB = default_library()
ENLX = default_sample_enlx()
HAS_REAL_LIB = bool(LIB) and os.path.exists(LIB)


class SanitizeTests(unittest.TestCase):
    def test_forbidden_chars(self):
        self.assertNotIn(":", sanitize_filename("a:b*c?d"))
        self.assertNotIn("/", sanitize_filename("a/b"))
        self.assertTrue(sanitize_filename("").strip())

    def test_reserved_device_name(self):
        self.assertEqual(sanitize_filename("CON"), "_CON")

    def test_length_cap(self):
        self.assertLessEqual(len(sanitize_filename("x" * 500, max_len=100)), 100)

    def test_surname(self):
        self.assertEqual(surname_of("Shannon, R. J."), "Shannon")
        self.assertEqual(surname_of("van der Waals, J. D."), "van der Waals")
        self.assertEqual(surname_of("牛加新"), "牛加新")

    def test_clean_title(self):
        self.assertEqual(clean_title("A <i>nice</i> title"), "A nice title")
        self.assertEqual(clean_title("<some-file.pdf>"), "")
        self.assertEqual(clean_title("line\rbreak"), "line break")


@unittest.skipUnless(HAS_REAL_LIB, "no EndNote library available (set ENDNOTE_LIBRARY)")
class LibraryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.lib = EndNoteLibrary(LIB)

    @classmethod
    def tearDownClass(cls):
        cls.lib.close()

    def test_counts_are_consistent(self):
        stats = self.lib.stats()
        self.assertGreater(stats["references"], 0)
        self.assertEqual(stats["references"], stats["active"] + stats["trash"])
        # every PDF attachment is either on disk or reported as missing
        total = sum(len(r.pdfs) for r in self.lib.active_references)
        self.assertEqual(total, stats["pdf_files"] + stats["missing_pdf"])
        self.assertGreaterEqual(stats["with_pdf"], 0)

    def test_unfiled_is_consistent(self):
        filed = set()
        for group in self.lib.custom_groups:
            filed.update(group.member_ids)
        expected = [r for r in self.lib.active_references if r.id not in filed]
        self.assertEqual(len(self.lib.unfiled_references()), len(expected))

    def test_group_membership(self):
        custom = self.lib.custom_groups
        if not custom:
            self.skipTest("library has no custom groups")
        group = max(custom, key=lambda g: len(g.member_ids))
        self.assertGreater(len(group.member_ids), 0)
        self.assertEqual(len(self.lib.refs_in_group(group.id)), len(group.member_ids))
        # every member id must resolve to a record
        self.assertTrue(all(i in self.lib.by_id for i in group.member_ids))

    def test_groups_are_named(self):
        for group in self.lib.custom_groups:
            self.assertTrue(group.name.strip())
            self.assertEqual(group.kind, "custom")

    def test_pdf_paths_exist(self):
        attachments = [a for r in self.lib.active_references for a in r.pdfs]
        existing = [a for a in attachments if a.exists]
        self.assertEqual(len(existing) + self.lib.stats()["missing_pdf"], len(attachments))
        for att in existing[:20]:
            self.assertTrue(os.path.isfile(att.disk_path()))

    def test_missing_attachment_is_flagged(self):
        broken = [a for r in self.lib.active_references for a in r.pdfs if not a.exists]
        if not broken:
            self.skipTest("library currently has no broken attachment")
        att = broken[0]
        self.assertFalse(att.exists)
        self.assertFalse(os.path.exists(att.disk_path()))

    def test_search(self):
        sample = next(
            (r for r in self.lib.active_references if len(r.title.split()) >= 2),
            None,
        )
        if sample is None:
            self.skipTest("no suitable reference")
        term = " ".join(sample.title.split()[:2])
        hits = EndNoteLibrary.search(term, self.lib.active_references)
        self.assertIn(sample.id, [r.id for r in hits])

    def test_recent(self):
        recent = self.lib.recent_references(3)
        self.assertEqual(len(recent), min(3, len(self.lib.active_references)))
        if len(recent) >= 2:
            self.assertGreaterEqual(recent[0].added_ts, recent[1].added_ts)


@unittest.skipUnless(os.path.exists(ENLX), "sample compressed library not available")
class CompressedLibraryTests(unittest.TestCase):
    def test_enlx(self):
        lib = EndNoteLibrary(ENLX)
        try:
            self.assertGreater(lib.stats()["active"], 0)
            ref = next((r for r in lib.active_references if r.has_pdf), None)
            if ref is None:
                self.skipTest("sample library has no PDF attachment")
            pdf = ref.pdfs[0]
            self.assertTrue(pdf.exists)
            self.assertEqual(pdf.read_bytes()[:4], b"%PDF")
        finally:
            lib.close()


@unittest.skipUnless(HAS_REAL_LIB, "no EndNote library available (set ENDNOTE_LIBRARY)")
class NamingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.lib = EndNoteLibrary(LIB)

    @classmethod
    def tearDownClass(cls):
        cls.lib.close()

    def _ref(self):
        ref = next(
            (r for r in self.lib.active_references
             if r.year and r.first_author and r.title),
            None,
        )
        if ref is None:
            self.skipTest("library has no complete reference")
        return ref

    def test_year_author_title(self):
        ref = self._ref()
        name = build_base_name(ref, NamingOptions(scheme="year_author_title"), 110)
        self.assertTrue(name.startswith(ref.year + "-" + ref.first_author))
        # the title's first word (after Windows-safe sanitising) is included
        first_word = sanitize_filename(ref.title.split()[0], 40)
        self.assertIn(first_word, name)

    def test_author_year_title(self):
        ref = self._ref()
        name = build_base_name(ref, NamingOptions(scheme="author_year_title"), 110)
        self.assertTrue(name.startswith(ref.first_author + "-" + ref.year))

    def test_custom_template(self):
        ref = self._ref()
        name = build_base_name(
            ref, NamingOptions(scheme="custom", template="{year}_{id}_{author}"), 110
        )
        self.assertEqual(name, "%s_%d_%s" % (ref.year, ref.id, ref.first_author))

    def test_original_scheme_returns_none(self):
        ref = self._ref()
        self.assertIsNone(build_base_name(ref, NamingOptions(scheme="original"), 110))


@unittest.skipUnless(HAS_REAL_LIB, "no EndNote library available (set ENDNOTE_LIBRARY)")
class ExportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.lib = EndNoteLibrary(LIB)

    @classmethod
    def tearDownClass(cls):
        cls.lib.close()

    def _items(self):
        """A small group of references that actually carry PDFs."""
        refs = [r for r in self.lib.active_references if r.has_pdf]
        if not refs:
            self.skipTest("library has no PDF attachments")
        chosen = refs[:5]
        membership = {}
        for group in self.lib.custom_groups:
            for ref_id in group.member_ids:
                membership.setdefault(ref_id, []).append(group.name)
        return [
            ExportItem(
                reference=ref,
                library="testlib",
                groups=tuple(membership.get(ref.id, ())),
            )
            for ref in chosen
        ]

    def test_export_some_pdfs(self):
        items = self._items()
        expected = sum(len([a for a in it.reference.pdfs if a.exists]) for it in items)
        out = tempfile.mkdtemp(prefix="en2ima_test_")
        try:
            result = export_items(items, ExportOptions(out_dir=out))
            self.assertEqual(result.count, expected)
            self.assertGreater(result.total_bytes, 0)
            files = [f for f in os.listdir(out) if f.lower().endswith(".pdf")]
            self.assertEqual(len(files), expected)
            self.assertFalse(result.errors)
            for name in files[:5]:
                with open(os.path.join(out, name), "rb") as fh:
                    self.assertEqual(fh.read(4), b"%PDF")
            # names are unique
            self.assertEqual(len({f.name.lower() for f in result.files}), result.count)
        finally:
            shutil.rmtree(out, ignore_errors=True)

    def test_manifest_written(self):
        items = self._items()
        out = tempfile.mkdtemp(prefix="en2ima_manifest_")
        try:
            result = export_items(items, ExportOptions(out_dir=out, write_manifest=True))
            self.assertTrue(os.path.isfile(result.manifest_path))
            with open(result.manifest_path, encoding="utf-8-sig") as fh:
                lines = [ln for ln in fh.read().splitlines() if ln.strip()]
            self.assertEqual(len(lines), result.count + 1)
        finally:
            shutil.rmtree(out, ignore_errors=True)

    def test_original_names_and_multi_pdf(self):
        multi = [
            r for r in self.lib.active_references
            if len([a for a in r.pdfs if a.exists]) > 1
        ]
        ref = multi[0] if multi else next(
            (r for r in self.lib.active_references if r.has_pdf), None
        )
        if ref is None:
            self.skipTest("library has no PDF attachments")
        items = [ExportItem(reference=ref, library="testlib")]
        out = tempfile.mkdtemp(prefix="en2ima_orig_")
        try:
            result = export_items(
                items, ExportOptions(out_dir=out, naming=NamingOptions(scheme="original"))
            )
            self.assertEqual(result.count, len([a for a in ref.pdfs if a.exists]))
            self.assertEqual(len({f.name.lower() for f in result.files}), result.count)
        finally:
            shutil.rmtree(out, ignore_errors=True)

    def test_missing_attachment_reported(self):
        broken = [
            (r, a) for r in self.lib.active_references for a in r.pdfs if not a.exists
        ]
        if not broken:
            self.skipTest("library currently has no broken attachment")
        ref = broken[0][0]
        expected_missing = sum(1 for a in ref.pdfs if not a.exists)
        items = [ExportItem(reference=ref, library="testlib")]
        out = tempfile.mkdtemp(prefix="en2ima_missing_")
        try:
            result = export_items(items, ExportOptions(out_dir=out))
            self.assertEqual(len(result.missing), expected_missing)
            self.assertEqual(result.count, len([a for a in ref.pdfs if a.exists]))
        finally:
            shutil.rmtree(out, ignore_errors=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
