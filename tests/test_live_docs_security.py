"""Live Assistant documents: the security review's regression tests.

Each test pins a hole that was found and closed (doc_reader.py, live_docs.py), with the
hostile input that opened it:

* a zip whose list of parts is enormous (a 47 MB file of a million tiny parts cost 14 s and
  400 MB inside zipfile before any cap of ours could look);
* a reader stuck in Python code that was "abandoned" but kept a CPU core (a 13 KB PDF whose
  one page takes an hour to extract);
* a list of documents that is locked for a moment (a virus scanner) being taken for empty,
  which swept every stored document away;
* a locked file that could not be deleted when the person removed the document;
* an engine kind that is neither local nor cloud being taken for an agreed cloud route;
* pictures going out whole with an automatic answer nobody asked for;
* pypdf's log lines (they quote names out of the document) reaching transcribe.log;
* a 40-megapixel picture needing 730 MB of memory.

Qt-free like tests/test_live_docs.py: it runs in CI's Ubuntu job too.
"""
import builtins
import io
import logging
import struct
import tempfile
import threading
import time
import unittest
import zipfile
import zlib
from pathlib import Path
from unittest import mock

import doc_reader
import live_docs
from tests import live_docs_fixtures as fx
from tests.test_live_docs import (REAL_PIL, needs_pil, needs_pypdf, read, setUpModule,  # noqa: F401
                                  tearDownModule)                                      # (module hooks)


# ── invisible text (a way to hide instructions in a document) ──
HIDDEN = ("Visible ‮evil‬ ​zero‍width  line \u0085next "
          "\U000e0041\U000e0042tag ⁦iso⁩ ﻿bom ­soft ؜arab ⁠join")
INVISIBLE = ("‮", "‬", "​", "‍", " ", "\u0085", "\U000e0041", "⁦",
             "⁩", "﻿", "­", "؜", "⁠")


def text_of_result(r):
    return "\n".join(t for _l, t in r.sections)


class TestInvisibleText(unittest.TestCase):
    """XML allows bidi overrides, zero-width characters and "tag" characters (U+E0000: a whole
    invisible alphabet), so a .docx can carry text no person sees and a model reads. Every
    format loses them - the Office and OpenDocument readers used to keep them."""

    def assert_clean(self, r, label):
        self.assertTrue(r.ok, (label, r.message))
        text = text_of_result(r)
        for ch in INVISIBLE:
            self.assertNotIn(ch, text, "%s kept U+%04X" % (label, ord(ch)))
        self.assertIn("Visible", text)
        self.assertNotIn("\r", text)

    def test_every_format_loses_the_characters_nobody_can_see(self):
        builds = {
            "docx": fx.make_docx([HIDDEN]),
            "pptx": fx.make_pptx([HIDDEN], notes={1: HIDDEN}),
            "xlsx": fx.make_xlsx({"S": [[HIDDEN]]}),
            "odt": fx.make_odt([HIDDEN]),
            "odp": fx.make_odp([HIDDEN]),
            "ods": fx.make_ods({"S": [[HIDDEN]]}),
            "epub": fx.make_epub([HIDDEN]),
            "txt": HIDDEN.encode("utf-8"),
            "html": b"<p>" + HIDDEN.encode("utf-8") + b"</p>",
        }
        for ext, data in builds.items():
            with self.subTest(ext):
                self.assert_clean(read(data, "a." + ext), ext)

    def test_rtf_too(self):
        bmp = HIDDEN.replace("\U000e0041\U000e0042", "")
        self.assert_clean(read(fx.make_rtf(bmp), "a.rtf"), "rtf")

    def test_a_stored_text_is_cleaned_again_on_its_way_to_the_model(self):
        """A text kept by an older version, or edited on disk, is not trusted either: no line
        separator or tag character reaches the quoted block."""
        d = live_docs.DocText("d1", "old.txt", "text", [("", "Visible budget line" + HIDDEN)])
        block = live_docs.documents_context([d], "visible budget", "", 3000)
        self.assertIn("Visible budget line", block)
        for ch in INVISIBLE:
            self.assertNotIn(ch, block)
        lines = block.split("\n")
        inside = lines[lines.index(live_docs.DOC_BEGIN) + 1:lines.index(live_docs.DOC_END)]
        for line in inside:
            self.assertTrue(line.startswith(("> ", "[from ")) or not line, repr(line))

    def test_the_cleaning_changes_nothing_else(self):
        self.assertEqual(doc_reader.strip_invisible("a\r\nb\rc\td  e"), "a\nb\nc\td  e")
        self.assertEqual(doc_reader.strip_invisible(""), "")
        self.assertEqual(doc_reader.strip_invisible("Ärger, 日本語, Բարև, ‍ok \U0001f600"),
                         "Ärger, 日本語, Բարև, ok \U0001f600")


class TestTextWindow(unittest.TestCase):
    """A text-like file is decoded only as far as the character budget can use: a 50 MB one
    took 700-900 MB of memory and up to 27 seconds to turn into text."""

    def test_a_big_text_file_is_cut_and_still_decoded_right(self):
        text = ("Слово о бюджете. " * 30000).encode("utf-8")
        with mock.patch.object(doc_reader, "TEXT_BYTES_MAX", 100_001):    # cuts inside a 2-byte letter
            r = read(text, "a.txt")
        self.assertTrue(r.ok)
        self.assertTrue(r.truncated)
        body = text_of_result(r)
        self.assertTrue(body.startswith("Слово о бюджете."))
        self.assertLess(len(body), 60_000)
        self.assertLessEqual(body.count("�"), 1)

    def test_html_and_rtf_are_cut_too(self):
        with mock.patch.object(doc_reader, "TEXT_BYTES_MAX", 20_000):
            html = read(b"<p>Hello there.</p>" * 5000, "a.html")
            rtf = read(b"{\\rtf1 " + b"Hello there.\\par " * 5000 + b"}", "a.rtf")
            small = read(b"Hello there.", "a.txt")
        for r in (html, rtf):
            self.assertTrue(r.ok and r.truncated)
            self.assertIn("Hello there.", text_of_result(r))
        self.assertTrue(small.ok and not small.truncated)

    def test_a_big_file_in_a_zip_is_not_cut_by_this(self):
        r = read(fx.make_docx(["Hello there."] * 3), "a.docx")
        self.assertTrue(r.ok and not r.truncated)


# ── the reply path, on the GUI thread ──
class TestReplyCleaningIsLinear(unittest.TestCase):
    """An answer is cleaned on the GUI thread, again every 120 ms while it streams. "<" followed
    by thousands of blanks made the old pattern backtrack quadratically: 8000 blanks froze the
    card for 14 seconds, 20000 for a minute and a half."""

    def timed(self, text):
        import live_context
        t = time.time()
        out = live_context.safe_answer_markdown(text)
        return out, time.time() - t

    def test_a_lot_of_blanks_after_a_bracket_cost_nothing(self):
        n = 30000
        for text in ("<" + " " * n + "x", "<" + " " * n + "/" + " " * n + "x", "< " * n,
                     ("<" + " " * 50) * (n // 50), "<" + "\t\n " * n, "<img " * n,
                     "![" + "a\n" * n, "![" + "[" * n, "![a](" + "(" * n):
            _out, took = self.timed(text)
            self.assertLess(took, 2.0, repr(text[:12]))

    def test_the_tags_that_load_something_are_still_removed(self):
        out, _t = self.timed("a <img src=x> b < img src=y> c <  /  IMG src=z> d <svg onload=1> e "
                             "<iframe src=//a/b> f ![x](u) g")
        for word in ("img", "iframe", "svg", "src="):
            self.assertNotIn(word, out.lower())
        for kept in ("a", "b", "c", "d", "e", "f", "g"):
            self.assertIn(" %s " % kept, " " + out + " ")


# ── hostile zips, built byte by byte ──
def zip_with_entries(count, name=b"a"):
    """A zip file that is nothing but ``count`` central-directory entries (47 bytes each)."""
    one = struct.pack("<IHHHHHHIIIHHHHHII", 0x02014b50, 20, 20, 0, 0, 0, 0, 0, 0, 0, len(name),
                      0, 0, 0, 0, 0, 0) + name
    directory = one * count
    head = b"PK\x03\x04" + b"\0" * 26
    end = struct.pack("<IHHHHIIH", 0x06054b50, 0, 0, min(count, 0xFFFF), min(count, 0xFFFF),
                      len(directory), len(head), 0)
    return head + directory + end


def zip64_claiming(directory_size):
    """A zip64 file whose end records say the list of parts is ``directory_size`` bytes."""
    head = b"PK\x03\x04" + b"\0" * 26
    z64 = struct.pack("<4sQHHIIQQQQ", b"PK\x06\x06", 44, 45, 45, 0, 0, 1, 1, directory_size,
                      len(head))
    locator = struct.pack("<4sIQI", b"PK\x06\x07", 0, len(head), 1)
    end = struct.pack("<4sHHHHIIH", b"PK\x05\x06", 0, 0, 0xFFFF, 0xFFFF, 0xFFFFFFFF, 0xFFFFFFFF, 0)
    return head + z64 + locator + end


class TestZipDirectory(unittest.TestCase):
    """The list of parts is measured BEFORE zipfile builds an object for each of them."""

    def refused_without_zipfile(self, data, name="x.docx"):
        with mock.patch.object(zipfile, "ZipFile", side_effect=AssertionError("zipfile was asked")):
            t = time.time()
            r = read(data, name)
        self.assertFalse(r.ok)
        self.assertEqual(r.reason, "unsafe")
        self.assertLess(time.time() - t, 3)
        return r

    def test_a_million_tiny_parts_are_refused_before_zipfile_sees_them(self):
        data = zip_with_entries(1_000_000)
        self.assertGreater(len(data), 40_000_000)
        self.assertLess(len(data), doc_reader.FILE_BYTES_MAX)       # under the size cap: it got this far
        self.refused_without_zipfile(data)

    def test_a_list_just_over_the_cap_is_refused_and_one_under_it_is_still_a_zip(self):
        over = (doc_reader.ZIP_DIRECTORY_MAX // 47) + 100
        self.refused_without_zipfile(zip_with_entries(over))
        under = zip_with_entries(1000)                             # a zip, with nothing in it we read
        self.assertLess(doc_reader._directory_size(under), doc_reader.ZIP_DIRECTORY_MAX)
        self.assertNotEqual(read(under, "x.docx").reason, "unsafe")

    def test_a_zip64_record_that_claims_a_huge_list_is_believed(self):
        self.refused_without_zipfile(zip64_claiming(40_000_000))
        self.assertEqual(doc_reader._directory_size(zip64_claiming(1234)), 1234)

    def test_it_measures_what_zipfile_would_read(self):
        data = fx.make_docx(["Hello there"])
        zf = zipfile.ZipFile(io.BytesIO(data))
        self.assertEqual(doc_reader._directory_size(data), len(data) - 22 - zf.start_dir)

    def test_a_zip_comment_does_not_hide_the_end_record(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("word/document.xml", fx.make_docx(["x"]))
            zf.comment = b"c" * 5000 + b"PK\x05\x06"[:3]
        data = buf.getvalue()
        size = doc_reader._directory_size(data)
        self.assertGreater(size, 0)
        self.assertLess(size, doc_reader.ZIP_DIRECTORY_MAX)

    def test_no_end_record_is_not_a_crash(self):
        self.assertEqual(doc_reader._directory_size(b"PK\x03\x04" + b"\0" * 100), -1)
        self.assertEqual(doc_reader._directory_size(b""), -1)
        r = read(b"PK\x03\x04" + b"\0" * 100, "x.docx")
        self.assertEqual(r.reason, "damaged")
        # a truncated end record
        self.assertEqual(doc_reader._directory_size(b"PK\x03\x04" + b"PK\x05\x06" + b"\0" * 5), -1)

    def test_a_real_document_with_thousands_of_parts_is_still_read(self):
        body = zipfile.ZipFile(io.BytesIO(fx.make_docx(["Kept text."]))).read("word/document.xml")
        parts = {"word/document.xml": body}
        for i in range(4000):
            parts["customXml/item%05d.xml" % i] = b"<a/>"
        r = read(fx.make_zip(parts), "many.docx")
        self.assertTrue(r.ok, r.message)
        self.assertIn("Kept text.", " ".join(t for _l, t in r.sections))


# ── a reader that is given up on is stopped ──
def alive_readers():
    return [t for t in threading.enumerate() if t.name == "gusan-doc-reader" and t.is_alive()]


def wait_until(cond, seconds=10.0):
    end = time.monotonic() + seconds
    while not cond() and time.monotonic() < end:
        time.sleep(0.02)
    return cond()


def slow_pdf(megabytes):
    """A few KB of file whose one page takes (much more than) minutes to extract."""
    unit = b"BT /F1 12 Tf 72 720 Td (hello world) Tj 0 -14 Td (second line here) Tj ET\n"
    packed = zlib.compress(unit * int(megabytes * 1e6 // len(unit)), 9)
    base = fx.make_pdf([""])
    empty = b"4 0 obj\n<< /Length 0 >>\nstream\n\nendstream\nendobj\n"
    assert empty in base
    return base.replace(empty, b"4 0 obj\n<< /Length %d /Filter /FlateDecode >>\nstream\n" % len(packed)
                        + packed + b"\nendstream\nendobj\n")


class TestAbandonedReaders(unittest.TestCase):
    def test_a_reader_stuck_in_python_code_is_stopped_not_just_forgotten(self):
        ended = threading.Event()

        def spin(budget):
            try:
                while True:
                    try:
                        sum(range(200))
                    except Exception:               # a parser that swallows its errors
                        pass
            finally:
                ended.set()
        with mock.patch.object(doc_reader, "_GRACE_S", 0.1):
            r = doc_reader._guarded(spin, None, 0.1)
        self.assertEqual(r.reason, "timeout")
        self.assertTrue(ended.wait(5), "the abandoned reader kept running")
        self.assertTrue(wait_until(lambda: doc_reader._abandoned_running() == 0))

    def test_cancel_stops_a_reader_that_does_not_look_at_the_budget(self):
        ended = threading.Event()
        started = time.monotonic()

        def spin(budget):
            try:
                while True:
                    sum(range(200))
            finally:
                ended.set()
        r = doc_reader._guarded(spin, lambda: time.monotonic() - started > 0.3, 30)
        self.assertEqual(r.reason, "cancelled")
        self.assertTrue(ended.wait(5), "a cancelled reader kept running")

    def test_the_stop_makes_no_noise(self):
        """No traceback on the console or in a log when a reader is stopped."""
        seen = []
        ended = threading.Event()

        def spin(budget):
            try:
                while True:
                    sum(range(200))
            finally:
                ended.set()
        with mock.patch.object(threading, "excepthook", lambda args: seen.append(args)), \
                mock.patch.object(doc_reader, "_GRACE_S", 0.1):
            doc_reader._guarded(spin, None, 0.1)
            self.assertTrue(ended.wait(5))
            time.sleep(0.3)
        self.assertEqual(seen, [])

    @needs_pypdf
    def test_a_small_pdf_with_an_endless_page_does_not_keep_a_core(self):
        pdf = slow_pdf(6)
        self.assertLess(len(pdf), 100_000)
        with mock.patch.object(doc_reader, "_GRACE_S", 0.3):
            r = read(pdf, "slow.pdf", time_budget=1)
        self.assertEqual(r.reason, "timeout")
        self.assertTrue(wait_until(lambda: not alive_readers(), 10),
                        "the reader of an abandoned PDF is still running")

    def test_while_stopped_readers_are_still_running_no_new_file_is_started(self):
        gate = threading.Event()
        stuck = [threading.Thread(target=gate.wait, daemon=True) for _ in range(doc_reader._ABANDONED_MAX)]
        for t in stuck:
            t.start()
        try:
            with mock.patch.object(doc_reader, "_ABANDONED", list(stuck)):
                r = read(b"hello there", "a.txt")
                self.assertEqual((r.ok, r.reason), (False, "busy"))
                self.assertTrue(r.message and "limit" not in r.message.lower())
                gate.set()
                for t in stuck:
                    t.join(2)
                self.assertTrue(read(b"hello there", "a.txt").ok)          # they ended: reading resumes
        finally:
            gate.set()

    def test_the_late_stop_cannot_reach_a_finished_read(self):
        """A normal read that ends well never sees the stop (it is raised only for a reader
        that was given up on)."""
        for _ in range(20):
            self.assertTrue(read(fx.make_docx(["Hello"]), "a.docx").ok)
        self.assertEqual(doc_reader._abandoned_running(), 0)


# ── the list of documents ──
class TestIndexBusy(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "live_docs"
        first = live_docs.DocStore(self.root)
        self.a = first.add_result("a.txt", read(b"Alpha text about budgets. " * 20, "a.txt")).info
        self.b = first.add_result("b.txt", read(b"Beta text about holidays. " * 20, "b.txt")).info
        self.assertTrue(self.a and self.b)
        self.busy = False
        real_open = builtins.open
        outer = self

        def flaky(file, *args, **kwargs):
            if outer.busy and str(file).endswith("index.json"):
                raise PermissionError(13, "The process cannot access the file (a virus scanner)")
            return real_open(file, *args, **kwargs)
        patcher = mock.patch("builtins.open", flaky)
        patcher.start()
        self.addCleanup(patcher.stop)

    def stored_files(self):
        return sorted(p.name for p in self.root.iterdir() if p.name.startswith("d"))

    def test_a_locked_list_is_not_an_empty_one_and_nothing_is_swept(self):
        before = self.stored_files()
        self.assertEqual(len(before), 2)
        store = live_docs.DocStore(self.root)                  # a fresh start: nothing cached
        self.busy = True
        self.assertEqual(store.list(), [])
        self.assertEqual(store.active_count(), 0)
        self.assertEqual(store.active_docs(), [])
        self.assertEqual(self.stored_files(), before)          # NOT swept as orphans
        self.busy = False
        self.assertEqual([d.name for d in store.list()], ["a.txt", "b.txt"])   # and they are all back

    def test_adding_while_the_list_is_locked_keeps_the_old_ones(self):
        store = live_docs.DocStore(self.root)
        self.busy = True
        res = store.add_result("c.txt", read(b"Gamma text about plans. " * 20, "c.txt"))
        self.assertFalse(res.ok)
        self.assertEqual(res.reason, "save")
        self.busy = False
        self.assertEqual([d.name for d in live_docs.DocStore(self.root).list()], ["a.txt", "b.txt"])

    def test_a_switch_while_locked_changes_nothing(self):
        store = live_docs.DocStore(self.root)
        self.busy = True
        self.assertFalse(store.set_active(self.a.id, False))
        self.busy = False
        self.assertTrue(all(d.active for d in store.list()))

    def test_removing_while_locked_still_deletes_the_files(self):
        store = live_docs.DocStore(self.root)
        self.busy = True
        self.assertTrue(store.remove(self.a.id))
        self.assertFalse(store.remove("../../outside"))        # only a real id is ever a file name
        self.assertEqual(self.stored_files(), [self.b.id + ".json"])
        self.busy = False
        fresh = live_docs.DocStore(self.root)
        self.assertEqual([d.name for d in fresh.list()], ["b.txt"])     # the stale row is dropped

    def test_clearing_while_locked_deletes_everything(self):
        store = live_docs.DocStore(self.root)
        self.busy = True
        store.clear()
        self.busy = False
        self.assertEqual(self.stored_files(), [])
        self.assertEqual(live_docs.DocStore(self.root).list(), [])

    def test_a_damaged_list_is_still_swept(self):
        """Not readable as JSON is a different thing from locked: nothing it listed can be
        shown or removed any more, so what it left behind is deleted, as before."""
        (self.root / "index.json").write_bytes(b"{ not json")
        store = live_docs.DocStore(self.root)
        self.assertEqual(store.list(), [])
        self.assertEqual(self.stored_files(), [])


class TestSwitchIsSavedOrNotChanged(unittest.TestCase):
    def test_a_switch_that_could_not_be_saved_is_not_shown_as_changed(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = live_docs.DocStore(Path(tmp) / "live_docs")
            info = store.add_result("a.txt", read(b"Alpha text about budgets. " * 20, "a.txt")).info
            with mock.patch.object(live_docs.DocStore, "_save", side_effect=OSError("disk full")):
                with self.assertRaises(OSError):
                    store.set_active(info.id, False)
            self.assertTrue(store.get(info.id).active)                  # what is shown is what is kept
            self.assertEqual([d.name for d in store.active_docs()], ["a.txt"])
            self.assertTrue(store.set_active(info.id, False))
            self.assertFalse(live_docs.DocStore(Path(tmp) / "live_docs").get(info.id).active)


class TestDeleteReallyDeletes(unittest.TestCase):
    def test_a_file_held_for_a_moment_is_deleted_on_a_later_try(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "d0123456789abcdef.json"
            path.write_text("{}")
            real, calls = __import__("os").unlink, []

            def flaky(p):
                calls.append(p)
                if len(calls) < 3:
                    raise PermissionError(13, "in use")
                real(p)
            with mock.patch("os.unlink", flaky), mock.patch.object(live_docs.time, "sleep"):
                live_docs._unlink(path)
            self.assertFalse(path.exists())
            self.assertEqual(len(calls), 3)

    def test_a_file_that_is_not_there_is_fine_and_a_stuck_one_gives_up(self):
        live_docs._unlink(Path(tempfile.gettempdir()) / "gusan-no-such-file-d0123")
        calls = []

        def stuck(p):
            calls.append(p)
            raise PermissionError(13, "in use")
        with mock.patch("os.unlink", stuck), mock.patch.object(live_docs.time, "sleep"):
            live_docs._unlink("whatever")
        self.assertEqual(len(calls), 4)                         # tried a few times, then left it to the sweep


# ── what may reach a model ──
class TestRouteKinds(unittest.TestCase):
    def test_only_a_local_or_a_cloud_route_is_ever_a_route_for_documents(self):
        agreed = {"live_docs_cloud_ok": True}
        for kind in (None, "", "rules", "unknown", "future", 0, "Cloud", "LOCAL_LLM"):
            use = live_docs.doc_use(agreed, kind, 3)
            self.assertEqual((use.send, use.ask, use.pictures), (False, False, False), kind)
        self.assertTrue(live_docs.doc_use(agreed, "managed", 3).send)
        self.assertTrue(live_docs.doc_use(agreed, "cloud", 3).send)
        self.assertTrue(live_docs.doc_use({}, "local_llm", 3).send)
        self.assertTrue(live_docs.doc_use({"privacy_mode": True}, "local_llm", 3).send)


class TestConsentIsForTheRouteItWasGivenFor(unittest.TestCase):
    """The question names the route ("Use your documents with Gusan Cloud?" / "...the AI service
    you added a key for?") and says different things about each, so a yes is a yes for that
    route only: Gusan Cloud, or ONE own-key service. Switching to another asks again."""

    CEREBRAS = "cloud:api_cerebras"

    def test_the_decision_table_by_route(self):
        use = live_docs.doc_use
        groq = "cloud:api_openai_compatible@api.groq.com"
        for agreed, kind, route, send in (
                ("managed", "managed", None, True), ("managed", "cloud", self.CEREBRAS, False),
                (self.CEREBRAS, "cloud", self.CEREBRAS, True), (self.CEREBRAS, "managed", None, False),
                (self.CEREBRAS, "cloud", "cloud:api_gemini", False),               # another provider
                ("cloud:api_openai_compatible", "cloud", groq, False),              # another address
                (groq, "cloud", groq, True),
                (self.CEREBRAS, "cloud", None, False),                              # a service nobody named
                ("cloud", "cloud", self.CEREBRAS, False), ("cloud", "cloud", None, False),  # (a dev build's)
                ("cloud", "managed", None, False),
                ("", "managed", None, True), ("", "cloud", self.CEREBRAS, True)):
            cfg = {"live_docs_cloud_ok": True, "live_docs_cloud_route": agreed}
            got = use(cfg, kind, 2, route)
            self.assertEqual((got.send, got.ask), (send, not send), (agreed, kind, route))
            self.assertEqual(got.pictures, send)
        self.assertTrue(use({"live_docs_cloud_ok": True, "live_docs_cloud_route": "managed"}, "local_llm", 2).send)
        self.assertTrue(use({"live_docs_cloud_ok": True}, "cloud", 2).send)           # (no route recorded: either)

    def test_a_route_is_one_service(self):
        import actions
        rid = live_docs.route_id
        self.assertEqual(rid("managed", actions.API_MANAGED_ID, {"action_api_base_url": "https://x.test"}),
                         "managed")                                                  # Gusan Cloud is one route
        self.assertEqual(rid("local_llm", "qwen"), "")
        self.assertEqual(rid(None), "")
        self.assertEqual(rid("cloud"), "cloud")                                      # (no engine: no service)
        self.assertEqual(rid("cloud", actions.API_CEREBRAS_ID, {}), self.CEREBRAS)
        openai = actions.API_OPENAI_ID
        self.assertEqual(rid("cloud", openai, {}), "cloud:" + openai)
        self.assertEqual(rid("cloud", openai, {"action_api_base_url": "https://api.openai.com/v1"}),
                         "cloud:" + openai)                                          # its own address
        self.assertEqual(rid("cloud", openai, {"action_api_base_url": "https://API.groq.com/openai/v1"}),
                         "cloud:%s@api.groq.com" % openai)
        self.assertEqual(rid("cloud", openai, {"action_api_base_url": "http://localhost:1234/v1"}),
                         "cloud:%s@localhost:1234" % openai)
        self.assertEqual(rid("cloud", actions.API_MISTRAL_ID, {"action_api_base_url": "https://api.mistral.ai/v1"}),
                         "cloud:" + actions.API_MISTRAL_ID)
        odd = rid("cloud", openai, {"action_api_base_url": "https://me:hunter2@x.test:99999/v1"})
        self.assertTrue(odd.startswith("cloud:%s@" % openai), odd)                  # (unreadable: still its own)
        self.assertNotIn("hunter2", odd)                                             # never what the URL holds
        self.assertNotEqual(rid("cloud", openai, {"action_api_base_url": "file:///v1"}), "cloud:" + openai)

    def test_a_no_stays_a_no_for_both(self):
        for here in ("managed", "cloud"):
            got = live_docs.doc_use({"live_docs_cloud_ok": False, "live_docs_cloud_route": "managed"}, here, 2)
            self.assertEqual((got.send, got.ask, got.why), (False, False, "declined"))

    def test_privacy_mode_is_still_absolute(self):
        for route in ("", "managed", "cloud", self.CEREBRAS):
            for here, rid in (("managed", "managed"), ("cloud", self.CEREBRAS), ("cloud", None)):
                cfg = {"privacy_mode": True, "live_docs_cloud_ok": True, "live_docs_cloud_route": route}
                self.assertEqual(live_docs.doc_use(cfg, here, 2, rid).why, "private")

    def test_with_choice_is_what_an_answer_in_flight_sees(self):
        base = {"live_docs_cloud_ok": True, "live_docs_cloud_route": "managed", "other": 1}
        yes = live_docs.with_choice(base, True, self.CEREBRAS)
        self.assertEqual((yes["live_docs_cloud_ok"], yes["live_docs_cloud_route"], yes["other"]),
                         (True, self.CEREBRAS, 1))
        self.assertTrue(live_docs.doc_use(yes, "cloud", 1, self.CEREBRAS).send)
        self.assertTrue(live_docs.doc_use(yes, "cloud", 1, "cloud:api_gemini").ask)      # one service
        no = live_docs.with_choice(base, False, self.CEREBRAS)
        self.assertEqual((no["live_docs_cloud_ok"], no["live_docs_cloud_route"]), (False, ""))
        self.assertEqual(base["live_docs_cloud_route"], "managed")                        # (a copy)
        self.assertEqual(live_docs.with_choice(None, True, "local_llm")["live_docs_cloud_route"], "")
        self.assertEqual(live_docs.with_choice(None, True, "managed")["live_docs_cloud_route"], "managed")

    def test_the_route_key_has_a_default_in_the_config(self):
        src = (Path(__file__).resolve().parent.parent / "main.py").read_text(encoding="utf-8")
        self.assertRegex(src, r'"%s":\s*""' % live_docs.CLOUD_ROUTE_KEY)


@needs_pil
class TestPicturesAreNotSentUnasked(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = live_docs.DocStore(Path(self.tmp.name) / "live_docs")
        for name in ("whiteboard.png", "invoice scan.png"):
            res = self.store.add_result(name, read(fx.make_png((200, 100), color=(len(name) * 3, 90, 30)), name))
            self.assertTrue(res.ok, res.message)

    def test_matching_only_takes_the_pictures_the_talk_is_about(self):
        names = lambda **kw: sorted(p.name for p in self.store.active_pictures(**kw))
        self.assertEqual(names(question="what is on the whiteboard?", matching_only=True), ["whiteboard.png"])
        self.assertEqual(names(question="payment terms for the supplier", matching_only=True), [])
        self.assertEqual(names(question="", matching_only=True), [])
        # a typed question may still take the newest ones, as before
        self.assertEqual(len(self.store.active_pictures("payment terms for the supplier")), 2)

    def test_a_switched_off_picture_is_never_taken(self):
        for d in self.store.list():
            self.store.set_active(d.id, False)
        self.assertEqual(self.store.active_pictures("whiteboard", matching_only=True), [])


# ── what is written to the log ──
@needs_pypdf
class TestPdfLog(unittest.TestCase):
    def test_what_pypdf_says_about_a_document_never_reaches_the_log(self):
        read(fx.make_pdf(["Hello there"]), "a.pdf")
        records = []
        handler = logging.Handler(logging.DEBUG)
        handler.emit = records.append
        root = logging.getLogger()
        root.addHandler(handler)
        old = root.level
        root.setLevel(logging.DEBUG)
        try:
            logging.getLogger("pypdf._cmap").error("Advanced encoding %s", "/NAME-FROM-THE-DOCUMENT")
            logging.getLogger("pypdf").critical("x")
            logging.getLogger("pypdf._reader").warning("Object 4 repaired near 'SECRET TEXT'")
        finally:
            root.setLevel(old)
            root.removeHandler(handler)
        self.assertEqual([r.getMessage() for r in records if r.name.startswith("pypdf")], [])


# ── memory ──
@needs_pil
class TestPicturesStayLight(unittest.TestCase):
    def test_a_picture_with_nothing_to_turn_is_not_copied_for_the_turning(self):
        from PIL import ImageOps
        with mock.patch.object(ImageOps, "exif_transpose", wraps=ImageOps.exif_transpose) as spy:
            r = read(fx.make_png((300, 200)), "a.png")
        self.assertTrue(r.ok)
        spy.assert_not_called()                                   # (it hands back a full-size COPY)

    def test_a_turned_photo_is_still_turned(self):
        from PIL import Image
        exif = Image.Exif()
        exif[0x0112] = 6                                          # rotated a quarter turn
        buf = io.BytesIO()
        Image.new("RGB", (300, 120), (200, 30, 30)).save(buf, "JPEG", exif=exif.tobytes())
        r = read(buf.getvalue(), "phone.jpg")
        self.assertTrue(r.ok)
        self.assertEqual((r.width, r.height), (120, 300))

    def test_a_palette_picture_with_a_clear_colour_is_flattened_on_white(self):
        from PIL import Image
        im = Image.new("P", (400, 300), 0)
        im.putpalette([255, 0, 0] + [0, 0, 0] * 255)
        buf = io.BytesIO()
        im.save(buf, "PNG", transparency=0)
        r = read(buf.getvalue(), "logo.png")
        self.assertTrue(r.ok, r.message)
        px = Image.open(io.BytesIO(r.image)).convert("RGB").getpixel((5, 5))
        self.assertTrue(all(c > 240 for c in px), px)

    def test_a_big_picture_is_shrunk_before_it_is_flattened(self):
        from PIL import Image
        seen = []
        real = Image.Image.convert

        def spy(self, mode=None, *a, **k):
            seen.append((self.size, mode))
            return real(self, mode, *a, **k)
        buf = io.BytesIO()
        Image.new("RGBA", (4000, 3000), (10, 200, 30, 255)).save(buf, "PNG")
        with mock.patch.object(Image.Image, "convert", spy):
            r = read(buf.getvalue(), "big.png")
        self.assertTrue(r.ok, r.message)
        self.assertLessEqual(max(r.width, r.height), doc_reader.IMAGE_SIDE)
        # (Pillow's own resize makes one premultiplied "RGBa" copy; ours - RGB, RGBA, L - are
        # made from the small picture)
        ours = [(size, mode) for size, mode in seen if mode in ("RGB", "RGBA", "L")]
        self.assertTrue(ours)
        self.assertTrue(all(max(size) <= doc_reader.IMAGE_SIDE for size, _m in ours),
                        "a full-size copy was made while flattening: %r" % (seen,))


if __name__ == "__main__":
    unittest.main()
