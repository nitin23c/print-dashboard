"""Tests for single modules, with no servers running."""
import gzip
import io
import os
import shutil
import sqlite3
import struct
import tempfile
import time
import unittest

from print_dashboard import ipp, pages, ppd
from print_dashboard.config import Settings
from print_dashboard.history import History
from print_dashboard.printer import find_problems
from print_dashboard.server import clean_name, whole_number
from tests.support import JPEG, PASSWORD, PPD, URF, encode_reply, pdf, png

HAS_PDFINFO = shutil.which("pdfinfo") is not None


class ConfigTests(unittest.TestCase):
    def test_defaults_match_the_cups_image(self):
        s = Settings.from_env({})
        self.assertEqual((s.cups_user, s.cups_password), ("cupsadmin", "cupsadmin"))
        self.assertEqual(s.cups_url, "http://127.0.0.1:631")

    def test_password_defaults_to_admin_name_like_the_cups_image(self):
        self.assertEqual(Settings.from_env({"CUPSADMIN": "boss"}).cups_password, "boss")

    def test_reads_everything(self):
        s = Settings.from_env({"CUPS_URL": "http://print:631/", "CUPS_QUEUE": "Q", "CUPSADMIN": "a", "CUPSPASSWORD": "b",
                               "PRINTER_IP": "printer.test", "DB_PATH": "/x/y.db", "PORT": "9000", "BIND_ADDRESS": "127.0.0.1",
                               "DASHBOARD_TITLE": "T", "DASHBOARD_NOTE": "N", "MAX_UPLOAD_MB": "5", "MAX_COPIES": "3", "KEEP_DAYS": "7"})
        self.assertEqual((s.cups_url, s.queue, s.cups_user, s.cups_password, s.printer_ip), ("http://print:631", "Q", "a", "b", "printer.test"))
        self.assertEqual((s.db_path, s.data_dir, s.port, s.bind_address), ("/x/y.db", "/x", 9000, "127.0.0.1"))
        self.assertEqual((s.title, s.note, s.max_upload_mb, s.max_copies, s.keep_days), ("T", "N", 5, 3, 7))

    def test_empty_values_use_defaults(self):
        s = Settings.from_env({"CUPS_URL": "", "PORT": "", "CUPSADMIN": ""})
        self.assertEqual((s.cups_url, s.port, s.cups_user), ("http://127.0.0.1:631", 8080, "cupsadmin"))

    def test_rejects_bad_numbers(self):
        for value in ("abc", "0", "-1", "1.5", "٣"):
            with self.assertRaises(ValueError):
                Settings.from_env({"PORT": value})

    def test_password_never_in_repr(self):
        text = repr(Settings(cups_password=PASSWORD))
        self.assertNotIn(PASSWORD, text)

    def test_number_error_does_not_echo_the_value(self):
        with self.assertRaises(ValueError) as caught:
            Settings.from_env({"PORT": "hunter2"})
        self.assertNotIn("hunter2", str(caught.exception))


class EncodeTests(unittest.TestCase):
    def test_request_bytes_match_the_ipp_format(self):
        body = ipp.encode(ipp.GET_PRINTER, "ipp://h/p", "u", fields=("a", "b"))
        self.assertEqual(body[:9], b"\x02\x00\x00\x0b\x00\x00\x00\x01\x01")
        self.assertEqual(body[-1], 0x03)
        self.assertIn(b"\x44\x00\x14requested-attributes\x00\x01a", body)
        self.assertIn(b"\x44\x00\x00\x00\x01b", body, "second value has an empty name")

    def test_required_attributes_come_first_and_in_order(self):
        body = ipp.encode(ipp.GET_PRINTER, "ipp://h/p", "u")
        positions = [body.index(n) for n in (b"attributes-charset", b"attributes-natural-language", b"printer-uri", b"requesting-user-name")]
        self.assertEqual(positions, sorted(positions))

    def test_job_group_follows_operation_group(self):
        body = ipp.encode(ipp.PRINT_JOB, "u", "u", extra=ipp.attr(ipp.NAME, "job-name", "n"), job=ipp.integer("copies", 2))
        self.assertLess(body.index(b"job-name"), body.index(b"copies"))
        self.assertIn(b"\x02\x21\x00\x06copies\x00\x04\x00\x00\x00\x02\x03", body)

    def test_round_trip_unicode(self):
        body = ipp.encode(ipp.PRINT_JOB, "ipp://h/p", "u", extra=ipp.attr(ipp.NAME, "job-name", "चालान ✓.pdf"))
        _, groups = ipp.parse_bytes(body)
        self.assertEqual(groups[0]["job-name"], ["चालान ✓.pdf"])


class ParseTests(unittest.TestCase):
    def test_values_of_each_type(self):
        code, groups = ipp.parse_bytes(encode_reply(0, [(ipp.PRINTER_GROUP, {"printer-state": 3, "color-supported": False, "printer-state-reasons": ["a", "b"]})]))
        printer = groups[1]
        self.assertEqual((code, printer["printer-state"], printer["color-supported"], printer["printer-state-reasons"]), (0, [3], [False], ["a", "b"]))

    def test_status_code(self):
        self.assertEqual(ipp.parse_bytes(encode_reply(0x0406))[0], 0x0406)

    def test_leaves_the_document_unread(self):
        stream = io.BytesIO(encode_reply(0) + b"document")
        ipp.parse(stream)
        self.assertEqual(stream.read(), b"document")

    def test_truncation_at_every_length_raises_cleanly(self):
        data = encode_reply(0, [(ipp.PRINTER_GROUP, {"printer-state": 3})])
        for size in range(len(data)):
            with self.assertRaises(ipp.IppError):
                ipp.parse_bytes(data[:size])

    def test_attribute_before_group(self):
        with self.assertRaises(ipp.IppError):
            ipp.parse_bytes(b"\x02\x00\x00\x00\x00\x00\x00\x01" + ipp.attr(ipp.KEYWORD, "a", "b"))

    def test_additional_value_with_no_name_first(self):
        data = b"\x02\x00\x00\x00\x00\x00\x00\x01\x01" + ipp.attr(ipp.KEYWORD, "", "orphan") + b"\x03"
        self.assertEqual(ipp.parse_bytes(data)[0], 0)

    def test_unknown_value_types_are_skipped(self):
        resolution = ipp.attr(0x32, "printer-resolution", b"\0" * 9)
        no_value = ipp.attr(0x13, "x", b"")
        data = b"\x02\x00\x00\x00\x00\x00\x00\x01\x04" + resolution + no_value + b"\x03"
        _, groups = ipp.parse_bytes(data)
        self.assertEqual(groups[0]["printer-resolution"], [])

    def test_bad_utf8_does_not_crash(self):
        data = b"\x02\x00\x00\x00\x00\x00\x00\x01\x04" + ipp.attr(ipp.NAME, "n", b"\xff\xfe") + b"\x03"
        self.assertEqual(len(ipp.parse_bytes(data)[1][0]["n"]), 1)

    def test_first(self):
        self.assertEqual(ipp.first({"a": [1, 2]}, "a"), 1)
        self.assertEqual(ipp.first({"a": []}, "a", 9), 9)
        self.assertEqual(ipp.first({}, "a", 9), 9)

    def test_real_reasons_drops_none_and_reports(self):
        reasons = {"printer-state-reasons": ["none", "media-empty-report", "media-empty-error"]}
        self.assertEqual(ipp.real_reasons(reasons), ["media-empty-error"])


class ClientUriTests(unittest.TestCase):
    def test_uri_uses_the_host_callers_gave(self):
        self.assertEqual(ipp.Client("http://print.test:631", "/printers/Q").uri, "ipp://print.test:631/printers/Q")


class DocumentTypeTests(unittest.TestCase):
    def test_accepted(self):
        self.assertEqual(pages.document_type(pdf()), "application/pdf")
        self.assertEqual(pages.document_type(png()), "image/png")
        self.assertEqual(pages.document_type(JPEG), "image/jpeg")

    def test_pdf_with_leading_junk(self):
        self.assertEqual(pages.document_type(b"\n\n junk \n%PDF-1.7\n"), "application/pdf")

    def test_refused(self):
        for data in (b"", b"plain text", b"<html><script>", b"PK\x03\x04word.docx", b"GIF89a", b"\x00\x00\x00\x18ftypheic",
                     b"RIFF....WEBP", b" " * 2000 + b"%PDF-1.4", b"\xff\xd8", b"\x89PNG"):
            self.assertIsNone(pages.document_type(data), data[:12])


class CountPagesTests(unittest.TestCase):
    def count(self, data):
        return pages.count_pages(io.BytesIO(data))

    def test_images_are_one_page(self):
        self.assertEqual(self.count(png()), 1)
        self.assertEqual(self.count(JPEG), 1)

    def test_urf_header(self):
        self.assertEqual(self.count(URF), 7)

    def test_urf_with_no_count_is_unknown(self):
        self.assertIsNone(self.count(b"UNIRAST\0\0\0\0\0"))

    def test_short_urf_is_unknown(self):
        self.assertIsNone(self.count(b"UNIRAST\0"))

    def test_unknown_and_empty(self):
        self.assertIsNone(self.count(b"something else"))
        self.assertIsNone(self.count(b""))

    def test_gzip_is_unpacked(self):
        self.assertEqual(self.count(gzip.compress(URF)), 7)

    def test_corrupt_gzip_raises_for_the_caller_to_handle(self):
        with self.assertRaises(OSError):
            self.count(b"\x1f\x8bnot really gzip")

    @unittest.skipUnless(HAS_PDFINFO, "pdfinfo not installed")
    def test_pdf(self):
        self.assertEqual(self.count(pdf(3)), 3)
        self.assertEqual(self.count(gzip.compress(pdf(4))), 4)

    @unittest.skipUnless(HAS_PDFINFO, "pdfinfo not installed")
    def test_broken_pdf_is_unknown(self):
        self.assertIsNone(self.count(b"%PDF-1.4 not a real pdf"))

    def test_no_pdfinfo_is_unknown(self):
        path = os.environ["PATH"]
        os.environ["PATH"] = ""
        self.addCleanup(os.environ.__setitem__, "PATH", path)
        self.assertIsNone(self.count(pdf()))


class PpdTests(unittest.TestCase):
    def test_gray_only_removes_every_route_to_colour(self):
        gray = ppd.gray_only(PPD)
        self.assertIn("*ColorDevice: False", gray)
        self.assertIn("*DefaultColorModel: Gray", gray)
        self.assertNotIn("ColorModel RGB", gray)
        self.assertIn("*ColorModel Gray", gray)
        self.assertIn('*APPrinterPreset Gray/Grey', gray)
        self.assertNotIn("APPrinterPreset Color", gray)

    def test_gray_only_is_stable(self):
        self.assertEqual(ppd.gray_only(ppd.gray_only(PPD)), ppd.gray_only(PPD))

    def test_job_chooses_keeps_colour_but_defaults_to_gray(self):
        chosen = ppd.job_chooses(PPD)
        self.assertIn("*ColorDevice: True", chosen)
        self.assertIn("*ColorModel RGB", chosen)
        self.assertIn("*DefaultColorModel: Gray", chosen)


class ProblemTests(unittest.TestCase):
    def test_each_problem(self):
        self.assertEqual(find_problems("media-empty-error"), ["Out of paper"])
        self.assertEqual(find_problems("trayEmptyOrOpen"), ["Out of paper"])
        self.assertEqual(find_problems("media-jam-error"), ["Paper jam"])
        self.assertEqual(find_problems("door-open-error"), ["Door or cover open"])
        self.assertEqual(find_problems("marker-supply-empty-error"), ["Ink system problem"])

    def test_several_at_once(self):
        self.assertEqual(find_problems("media-empty cover-open"), ["Out of paper", "Door or cover open"])

    def test_healthy(self):
        self.assertEqual(find_problems("ready inPowerSave"), [])


class HelperTests(unittest.TestCase):
    def test_clean_name(self):
        cases = {"report.pdf": "report.pdf", "a%20b.pdf": "a b.pdf", "%E0%A4%9A.pdf": "च.pdf", "": "Document", "   ": "Document",
                 "../../etc/passwd": "passwd", "..%2F..%2Fsecret.pdf": "secret.pdf", "C:\\Users\\me\\scan.pdf": "scan.pdf",
                 "a%0d%0aInjected: 1.pdf": "aInjected: 1.pdf", "%00.pdf": ".pdf", "dir/": "Document"}
        for raw, expected in cases.items():
            self.assertEqual(clean_name(raw), expected, raw)

    def test_whole_number(self):
        for values, expected in ((None, 1), (["3"], 3), (["0"], 1), (["99"], 20), (["-2"], 1), (["abc"], 1), ([""], 1), (["9" * 400], 1), (["٣"], 1)):
            self.assertEqual(whole_number(values, 1, 20), expected, values)


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.path = os.path.join(self.tmp, "h.db")
        self.history = History(self.path, keep_days=90)

    def job(self, job_id=1, state="completed", finished=None, name="a.pdf"):
        return dict(id=job_id, created=job_id, name=name, user="u", size_kb=1, state=state, finished=time.time() if finished is None else finished)

    def test_saves_and_lists_newest_first(self):
        now = time.time()
        self.history.save(self.job(1, finished=now - 10), 2)
        self.history.save(self.job(2, finished=now), 3)
        self.assertEqual([r["job_id"] for r in self.history.recent()], [2, 1])

    def test_saving_twice_keeps_one_row(self):
        self.history.save(self.job(), 2)
        self.history.save(self.job(), 2)
        self.assertEqual(len(self.history.recent()), 1)

    def test_totals_count_only_completed(self):
        self.history.save(self.job(1), 4)
        self.history.save(self.job(2, "cancelled"), 9)
        self.history.save(self.job(3, "failed"), 9)
        self.assertEqual(self.history.totals()["today"], {"jobs": 1, "pages": 4})

    def test_unknown_pages_count_as_zero(self):
        self.history.save(self.job(), None)
        self.assertEqual(self.history.totals()["today"]["pages"], 0)

    def test_today_and_month_boundaries(self):
        now = time.mktime((2026, 3, 15, 12, 0, 0, 0, 0, -1))
        self.history.save(self.job(1, finished=time.mktime((2026, 3, 15, 0, 0, 1, 0, 0, -1))), 1)
        self.history.save(self.job(2, finished=time.mktime((2026, 3, 14, 23, 59, 59, 0, 0, -1))), 2)
        self.history.save(self.job(3, finished=time.mktime((2026, 3, 1, 0, 0, 1, 0, 0, -1))), 4)
        self.history.save(self.job(4, finished=time.mktime((2026, 2, 28, 23, 59, 59, 0, 0, -1))), 8)
        # Those rows are old enough to be pruned, so keep them for this test.
        totals = History(self.path, keep_days=36500).totals(now)
        self.assertEqual((totals["today"]["pages"], totals["month"]["pages"]), (0, 0))
        fresh = History(os.path.join(self.tmp, "b.db"), keep_days=36500)
        for i, (finished, count) in enumerate(((time.mktime((2026, 3, 15, 0, 0, 1, 0, 0, -1)), 1), (time.mktime((2026, 3, 14, 23, 59, 59, 0, 0, -1)), 2),
                                               (time.mktime((2026, 3, 1, 0, 0, 1, 0, 0, -1)), 4), (time.mktime((2026, 2, 28, 23, 59, 59, 0, 0, -1)), 8))):
            fresh.save(self.job(i, finished=finished), count)
        totals = fresh.totals(now)
        self.assertEqual((totals["today"]["pages"], totals["month"]["pages"]), (1, 7))

    def test_old_rows_are_removed(self):
        self.history.save(self.job(1, finished=time.time() - 91 * 86400), 1)
        self.history.save(self.job(2), 1)
        self.assertEqual([r["job_id"] for r in self.history.recent()], [2])

    def test_recent_is_limited(self):
        for i in range(30):
            self.history.save(self.job(i), 1)
        self.assertEqual(len(self.history.recent()), 25)

    def test_sql_in_a_name_is_just_text(self):
        name = "x'); DROP TABLE jobs;--"
        self.history.save(self.job(name=name), 1)
        self.assertEqual(self.history.recent()[0]["name"], name)

    def test_opens_a_database_from_the_previous_version(self):
        db = sqlite3.connect(self.path)
        db.execute("INSERT INTO jobs VALUES (1,1,'old.pdf','u',1,'completed',1,?)", (time.time(),))
        db.commit()
        db.close()
        self.assertEqual(History(self.path).recent()[0]["name"], "old.pdf")


if __name__ == "__main__":
    unittest.main()
