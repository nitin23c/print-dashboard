"""Tests that run the real dashboard against a fake CUPS over real HTTP and IPP."""
import contextlib
import gzip
import http.client
import io
import json
import os
import shutil
import socket
import struct
import threading
import time
import unittest
import urllib.error

from print_dashboard import ipp
from print_dashboard.cups import Cups
from print_dashboard.history import History
from print_dashboard.monitor import Monitor
from print_dashboard.printer import Printer
from print_dashboard.server import create_server
from tests.support import ADMIN, JPEG, PASSWORD, PPD, URF, Case, pdf, png

HAS_PDFINFO = shutil.which("pdfinfo") is not None


class CupsClientTests(Case):
    def test_job_fields(self):
        self.fake.add_job("report.pdf", state=5, copies=2, user="mobile", done=1)
        job = self.cups.jobs("not-completed")[0]
        self.assertEqual((job["name"], job["state"], job["copies"], job["user"], job["done"]), ("report.pdf", "printing", 2, "mobile", 1))

    def test_each_job_state(self):
        for code, label in ((3, "waiting"), (4, "held"), (5, "printing"), (6, "paused")):
            self.fake.add_job(state=code)
        for code, label in ((7, "cancelled"), (8, "failed"), (9, "completed")):
            self.fake.add_job(state=code)
        self.assertEqual([j["state"] for j in self.cups.jobs("not-completed")], ["waiting", "held", "printing", "paused"])
        self.assertEqual([j["state"] for j in self.cups.jobs("completed")], ["cancelled", "failed", "completed"])

    def test_job_list_uses_the_admin_login(self):
        self.cups.jobs("not-completed")
        self.assertTrue(self.fake.requests[-1]["admin"])

    def test_strips_invisible_marks_ios_adds_to_names(self):
        self.fake.add_job("‎WhatsApp doc‏ ")
        self.assertEqual(self.cups.jobs("not-completed")[0]["name"], "WhatsApp doc")

    def test_wrong_password_hides_names_and_blocks_actions_like_real_cups(self):
        job_id = self.fake.add_job("secret.pdf")
        wrong = Cups(self.make_settings(cups_password="wrong"))
        self.assertEqual(wrong.jobs("not-completed")[0]["name"], "Document")
        with self.assertRaises(Exception) as caught:
            wrong.cancel(job_id)
        self.assertEqual(caught.exception.code, 401)

    def test_login_check(self):
        self.assertTrue(self.cups.login_ok())
        self.assertFalse(Cups(self.make_settings(cups_password="wrong")).login_ok())

    def test_queue_status(self):
        self.fake.reasons = ["none"]
        self.assertEqual(self.cups.queue(), {"online": True, "accepting": True, "color": True, "model": "Acme Inkjet 100", "stopped": False, "reasons": []})

    def test_queue_stopped(self):
        self.fake.state = 5
        self.assertTrue(self.cups.queue()["stopped"])

    def test_wrong_queue_name_is_a_clear_error(self):
        with self.assertRaises(LookupError):
            Cups(self.make_settings(queue="Nope")).queue()

    def test_uses_the_configured_address_in_the_printer_uri(self):
        self.cups.queue()
        self.assertEqual(self.fake.requests[-1]["attrs"]["printer-uri"], [f"ipp://127.0.0.1:{self.fake.port}/printers/{self.queue}"])

    def test_cancel(self):
        job_id = self.fake.add_job()
        self.assertTrue(self.cups.cancel(job_id))
        self.assertEqual(self.fake.jobs[job_id]["state"], 7)

    def test_cancel_missing_job(self):
        self.assertFalse(self.cups.cancel(999))

    def test_submit_pdf(self):
        self.assertTrue(self.cups.submit("a.pdf", pdf(), "application/pdf", 3, False))
        job = self.last_job()
        self.assertEqual((job["name"], job["copies"], job["data"], job["user"], job["format"]), ("a.pdf", 3, pdf(), "dashboard", "application/pdf"))
        self.assertEqual(job["options"]["print-color-mode"], ["monochrome"])
        self.assertNotIn("fit-to-page", job["options"])

    def test_submit_image_fills_the_page(self):
        self.cups.submit("a.png", png(), "image/png", 1, True)
        self.assertEqual(self.last_job()["options"]["fit-to-page"], [True])
        self.assertEqual(self.last_job()["options"]["print-color-mode"], ["color"])

    def test_submit_truncates_long_names_on_a_character_boundary(self):
        self.cups.submit("ब" * 200 + ".pdf", pdf(), "application/pdf", 1, False)
        name = self.last_job()["name"]
        self.assertLessEqual(len(name.encode()), 255)
        self.assertEqual(set(name), {"ब"})

    def test_submit_refused_by_queue(self):
        self.fake.accepting = False
        self.assertFalse(self.cups.submit("a.pdf", pdf(), "application/pdf", 1, False))

    def test_binary_document_arrives_intact(self):
        data = bytes(range(256)) * 4096
        self.cups.submit("b.jpg", b"\xff\xd8\xff" + data, "image/jpeg", 1, False)
        self.assertEqual(self.last_job()["data"], b"\xff\xd8\xff" + data)

    def test_job_pages_from_cups_with_no_shared_folder(self):
        self.assertEqual(self.cups.job_pages(self.fake.add_job(data=URF)), 7)
        self.assertEqual(self.cups.job_pages(self.fake.add_job(data=gzip.compress(URF))), 7)
        self.assertEqual(self.cups.job_pages(self.fake.add_job(data=png())), 1)

    def test_job_pages_when_file_is_gone(self):
        self.assertIsNone(self.cups.job_pages(999))

    def test_color_off_then_on_restores_the_queue(self):
        self.assertTrue(self.cups.set_color(False))
        self.assertFalse(self.fake.color)
        self.assertNotIn("ColorModel RGB", self.fake.ppd)
        self.assertTrue(self.cups.set_color(True))
        self.assertTrue(self.fake.color)
        self.assertIn("*ColorModel RGB", self.fake.ppd)
        self.assertIn("*DefaultColorModel: Gray", self.fake.ppd)

    def test_color_saved_copy_survives_a_restart(self):
        self.cups.set_color(False)
        self.assertTrue(Cups(self.settings).set_color(True))
        self.assertIn("*ColorModel RGB", self.fake.ppd)

    def test_color_on_a_grey_queue_with_no_saved_copy_fails_safely(self):
        self.fake.ppd = PPD.replace("*ColorDevice: True", "*ColorDevice: False")
        with self.assertRaises(FileNotFoundError):
            self.cups.set_color(True)

    def test_queue_name_with_spaces_is_quoted(self):
        self.assertEqual(Cups(self.make_settings(queue="My Printer")).client.path, "/printers/My%20Printer")


class PrinterTests(Case):
    def test_hp_printer(self):
        reading = self.printer.read()
        self.assertEqual((reading["online"], reading["sheets"], reading["mono"], reading["color"], reading["jams"], reading["misfeeds"]), (True, 500, 900, 100, 1, 2))

    def test_any_ipp_printer_works_without_hp_pages(self):
        self.fake.hp = False
        reading = self.printer.read()
        self.assertEqual(reading["online"], True)
        self.assertNotIn("sheets", reading)

    def test_ipp_problem_without_hp_pages(self):
        self.fake.hp = False
        self.fake.printer_reasons = ["media-empty-error"]
        self.assertEqual(self.printer.read()["problems"], ["Out of paper"])

    def test_hp_works_if_ipp_gives_no_printer_group(self):
        self.fake.printer_ipp = False
        self.assertTrue(self.printer.read()["online"])

    def test_no_address_means_queue_only(self):
        self.assertEqual(Printer("").read(), {"online": None, "problems": []})

    def test_unreachable(self):
        self.fake.close()
        self.addCleanup(lambda: None)
        self.assertEqual(self.printer.read()["online"], False)
        self.fake = type(self.fake)(self.queue)

    def test_stale_report_reason_is_not_a_problem(self):
        self.fake.printer_reasons = ["media-empty-report"]
        self.assertEqual(self.printer.read()["problems"], [])

    def test_hp_paper_out_and_sleep(self):
        self.fake.category = "trayEmpty"
        self.assertEqual(self.printer.read()["problems"], ["Out of paper"])
        self.fake.category = "inPowerSave"
        self.assertTrue(self.printer.read()["sleeping"])

    def test_notice_shown_unless_it_repeats_a_problem(self):
        self.fake.notice = "Align printhead"
        self.assertEqual(self.printer.read()["notices"], ["Align printhead"])
        self.fake.category, self.fake.notice = "jammed", "Paper jam"
        reading = self.printer.read()
        self.assertEqual((reading["problems"], reading["notices"]), (["Paper jam"], []))


class MonitorTests(Case):
    def test_waiting_job_gets_a_page_total(self):
        self.fake.add_job(data=URF, copies=2)
        self.settle()
        job = self.monitor.status()["jobs"][0]
        self.assertEqual((job["pages"], job["printed"]), (14, 0))

    def test_progress_comes_from_the_sheet_counter(self):
        job_id = self.fake.add_job(state=5, data=URF)
        self.settle()
        self.fake.sheets += 3
        self.poll()
        self.assertEqual(self.monitor.status()["jobs"][0]["printed"], 3)
        self.fake.jobs[job_id]["state"] = 9
        self.poll()
        self.assertEqual(self.history.recent()[0]["pages"], 7)

    def test_progress_comes_from_ipp_on_other_printers(self):
        self.fake.hp = False
        self.fake.add_job(state=5, done=4, data=URF)
        self.settle()
        self.assertEqual(self.monitor.status()["jobs"][0]["printed"], 4)

    def test_progress_never_goes_backwards(self):
        self.fake.add_job(state=5, data=URF)
        self.settle()
        self.fake.sheets += 3
        self.poll()
        self.fake.sheets -= 2
        self.poll()
        self.assertEqual(self.monitor.status()["jobs"][0]["printed"], 3)

    def test_job_with_no_retrievable_file_still_shows(self):
        self.fake.drop_document = True
        self.fake.add_job()
        self.settle()
        self.assertIsNone(self.monitor.status()["jobs"][0]["pages"])

    def test_page_count_gives_up_after_three_tries(self):
        self.fake.drop_document = True
        self.fake.add_job()
        for _ in range(8):
            self.settle()
        tries = [r for r in self.fake.requests if r["operation"] == ipp.GET_DOCUMENT]
        self.assertEqual(len(tries), 3)

    def test_counts_once_when_known(self):
        self.fake.add_job(data=URF)
        for _ in range(4):
            self.settle()
        self.assertEqual(len([r for r in self.fake.requests if r["operation"] == ipp.GET_DOCUMENT]), 1)

    def test_finished_job_moves_to_history_once(self):
        self.fake.add_job("done.pdf", state=9)
        self.poll()
        self.poll()
        self.assertEqual([r["name"] for r in self.history.recent()], ["done.pdf"])

    def test_cancelled_job_doesnt_count_its_whole_total(self):
        job_id = self.fake.add_job(state=5, data=URF)
        self.settle()
        self.fake.sheets += 2
        self.poll()
        self.fake.jobs[job_id]["state"] = 7
        self.poll()
        self.assertEqual(self.history.recent()[0]["pages"], 2)

    def test_finished_job_with_no_finish_time_is_kept(self):
        self.fake.jobs[self.fake.add_job()].update(state=9, finished=0)
        self.poll()
        self.assertEqual(len(self.history.recent()), 1)
        self.assertGreater(self.history.recent()[0]["finished"], 0)

    def test_tracking_is_freed_when_a_job_finishes(self):
        job_id = self.fake.add_job(data=URF)
        self.settle()
        self.fake.jobs[job_id]["state"] = 9
        self.poll()
        self.assertEqual(self.monitor.tracked, {})

    def test_reused_job_id_with_new_time_is_a_new_job(self):
        job_id = self.fake.add_job(data=URF)
        self.settle()
        self.fake.jobs[job_id].update(created=self.fake.jobs[job_id]["created"] + 100, data=png())
        self.settle()
        self.assertEqual(self.monitor.status()["jobs"][0]["pages"], 1)

    def test_stale_queue_reason_ignored_when_idle(self):
        self.fake.reasons = ["media-empty-error"]
        self.poll()
        self.assertEqual(self.monitor.status()["problems"], [])

    def test_queue_reason_counts_during_a_job(self):
        self.fake.reasons = ["media-empty-error"]
        self.fake.add_job()
        self.settle()
        self.assertEqual(self.monitor.status()["problems"], ["Out of paper"])

    def test_problem_is_not_listed_twice(self):
        self.fake.reasons = ["media-empty-error"]
        self.fake.printer_reasons = ["media-empty-error"]
        self.fake.add_job()
        self.settle()
        self.assertEqual(self.monitor.status()["problems"], ["Out of paper"])

    def test_cups_down_is_reported_then_recovers(self):
        self.poll()
        self.fake.fail_all = True
        self.poll()
        self.assertEqual(self.monitor.status()["queue"]["online"], False)
        self.fake.fail_all = False
        self.poll()
        self.assertTrue(self.monitor.status()["queue"]["online"])

    def test_wrong_password_is_named_in_the_status(self):
        monitor = Monitor(self.settings, Cups(self.make_settings(cups_password="wrong")), self.printer, self.history)
        monitor.poll()
        self.assertIs(monitor.status()["queue"]["login"], False)

    def test_good_password_reports_a_good_login(self):
        self.poll()
        self.assertIs(self.monitor.status()["queue"]["login"], True)

    def test_http_error_from_cups_names_the_login_if_refused(self):
        self.monitor.cups.jobs = lambda which: (_ for _ in ()).throw(urllib.error.HTTPError("u", 403, "no", None, None))
        self.poll()
        self.assertEqual(self.monitor.status()["queue"], {"online": False, "error": "login"})

    def test_nothing_listening(self):
        settings = self.make_settings(cups_url="http://127.0.0.1:9")
        monitor = Monitor(settings, Cups(settings), Printer(""), self.history)
        monitor.poll()
        self.assertEqual(monitor.status()["queue"], {"online": False, "error": "unreachable"})

    def test_wrong_queue_is_reported(self):
        settings = self.make_settings(queue="Nope")
        monitor = Monitor(settings, Cups(settings), self.printer, self.history)
        monitor.poll()
        self.assertEqual(monitor.status()["queue"]["error"], "unreachable")

    def test_printer_is_read_faster_during_a_job(self):
        self.poll()
        self.assertGreater(self.monitor.next_printer, time.time() + 20)
        self.fake.add_job()
        self.settle()
        self.assertLess(self.monitor.next_printer, time.time() + 5)

    def test_status_is_a_copy(self):
        self.fake.add_job()
        self.settle()
        self.monitor.status()["jobs"].clear()
        self.assertEqual(len(self.monitor.status()["jobs"]), 1)

    def test_status_never_contains_the_password(self):
        self.fake.add_job()
        self.settle()
        self.assertNotIn(PASSWORD, json.dumps(self.monitor.status()))

    def test_log_output_never_contains_the_password(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            Monitor(self.settings, Cups(self.make_settings(cups_password="wrong")), self.printer, self.history).poll()
            self.fake.fail_all = True
            self.poll()
        self.assertNotIn(PASSWORD, output.getvalue())
        self.assertNotIn("wrong", output.getvalue())


class ServerReadTests(Case):
    def test_page_and_assets(self):
        for path, kind in (("/", "text/html"), ("/app.js", "text/javascript"), ("/style.css", "text/css")):
            code, body, headers = self.request("GET", path)
            self.assertEqual(code, 200)
            self.assertIn(kind, headers["Content-Type"])
            self.assertGreater(len(body), 100)

    def test_security_headers_on_every_reply(self):
        for path in ("/", "/app.js", "/api/status", "/missing"):
            _, _, headers = self.request("GET", path)
            self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
            self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
            self.assertEqual(headers["Cache-Control"], "no-store")

    def test_page_has_no_inline_script_for_the_policy_to_block(self):
        _, body, _ = self.request("GET", "/")
        self.assertNotIn(b"<script>", body)
        self.assertNotIn(b"onclick", body)

    def test_healthz(self):
        self.assertEqual(self.api("GET", "/healthz"), (200, {"ok": True}))

    def test_status_shape(self):
        self.poll()
        code, status = self.api("GET", "/api/status")
        self.assertEqual(code, 200)
        self.assertEqual(status["title"], "Test Printer")
        self.assertEqual(status["limits"], {"upload_mb": 100, "copies": 20})
        self.assertEqual(set(status["totals"]), {"today", "month"})

    def test_status_before_the_first_poll(self):
        self.assertEqual(self.api("GET", "/api/status")[0], 200)

    def test_query_strings_are_ignored_on_reads(self):
        self.assertEqual(self.request("GET", "/app.js?v=2")[0], 200)

    def test_only_known_paths_are_served(self):
        for path in ("/print_dashboard/config.py", "/../config.py", "/static/app.js", "/.env", "/..%2fconfig.py", "/app.js/", "//etc/passwd"):
            self.assertEqual(self.request("GET", path)[0], 404, path)

    def test_no_directory_listing_or_traversal_over_a_raw_socket(self):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1])
        connection.putrequest("GET", "/../../etc/passwd", skip_host=True)
        connection.endheaders()
        self.assertEqual(connection.getresponse().status, 404)


class ServerActionTests(Case):
    def test_every_action_needs_the_header(self):
        job_id = self.fake.add_job()
        for path in (f"/api/cancel/{job_id}", "/api/color/off", "/api/print"):
            call = dict(body=pdf()) if path.endswith("print") else {}
            code, _, _ = self.request("POST", path, call.get("body", b""), {"X-Print-Dashboard": "0"})
            self.assertEqual(code, 403, path)
        self.assertEqual(self.fake.jobs[job_id]["state"], 3)
        self.assertTrue(self.fake.color)
        self.assertEqual(len(self.fake.jobs), 1)

    def test_get_cant_cause_actions(self):
        job_id = self.fake.add_job()
        self.assertEqual(self.request("GET", f"/api/cancel/{job_id}")[0], 404)
        self.assertEqual(self.fake.jobs[job_id]["state"], 3)

    def test_unknown_action(self):
        self.assertEqual(self.request("POST", "/api/reboot", b"")[0], 404)
        self.assertEqual(self.request("POST", "/api/color/purple", b"")[0], 404)
        self.assertEqual(self.request("POST", "/api/cancel/abc", b"")[0], 404)
        self.assertEqual(self.request("POST", "/api/cancel/" + "9" * 40, b"")[0], 404)

    def test_cancel(self):
        job_id = self.fake.add_job()
        self.assertEqual(self.api("POST", f"/api/cancel/{job_id}", b""), (200, {"ok": True}))

    def test_cancel_failure_reports_502(self):
        self.assertEqual(self.api("POST", "/api/cancel/999", b""), (502, {"ok": False}))

    def test_cancel_with_cups_down(self):
        self.fake.fail_all = True
        self.assertEqual(self.api("POST", "/api/cancel/1", b"")[0], 502)

    def test_color_toggle(self):
        self.poll()
        self.assertEqual(self.api("POST", "/api/color/off", b""), (200, {"ok": True}))
        self.poll()
        self.assertFalse(self.monitor.status()["queue"]["color"])
        self.assertEqual(self.api("POST", "/api/color/on", b""), (200, {"ok": True}))
        self.poll()
        self.assertTrue(self.monitor.status()["queue"]["color"])

    def test_color_locked_while_a_job_runs(self):
        self.fake.add_job()
        self.settle()
        self.assertEqual(self.api("POST", "/api/color/off", b""), (409, {"ok": False, "error": "busy"}))
        self.assertTrue(self.fake.color)

    def test_color_unlocks_on_the_poll_after_a_job_ends(self):
        job_id = self.fake.add_job()
        self.settle()
        self.fake.jobs[job_id]["state"] = 7
        self.poll()
        self.assertEqual(self.api("POST", "/api/color/off", b"")[0], 200)


class UploadTests(Case):
    def setUp(self):
        super().setUp()
        self.poll()

    def test_pdf_jpeg_and_png(self):
        for data, name, kind in ((pdf(), "a.pdf", "application/pdf"), (JPEG, "a.jpg", "image/jpeg"), (png(), "a.png", "image/png")):
            self.assertEqual(self.upload(data, name), (200, {"ok": True}))
            self.assertEqual((self.last_job()["format"], self.last_job()["data"], self.last_job()["name"]), (kind, data, name))

    def test_type_comes_from_contents_not_the_name(self):
        self.assertEqual(self.upload(b"MZ executable", "innocent.pdf")[0], 415)
        self.assertEqual(self.upload(png(), "wrong.pdf")[0], 200)
        self.assertEqual(self.last_job()["format"], "image/png")

    def test_empty_body(self):
        self.assertEqual(self.upload(b"")[0], 411)
        self.assertEqual(self.fake.jobs, {})

    def test_at_the_size_limit_and_one_byte_over(self):
        small = self.make_settings(max_upload_mb=1)
        server = create_server(small, self.cups, self.monitor)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        threading.Thread(target=server.serve_forever, args=(0.01,), daemon=True).start()
        self.base = "http://127.0.0.1:%d" % server.server_address[1]
        exact = JPEG + b"\0" * (1024 * 1024 - len(JPEG))
        self.assertEqual(self.upload(exact)[0], 200)
        code, reply = self.upload(exact + b"\0")
        self.assertEqual((code, reply["error"]), (413, "Files can be up to 1 MB."))

    def test_oversize_is_refused_from_the_header_without_reading_it(self):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1])
        connection.putrequest("POST", "/api/print")
        connection.putheader("Content-Length", str(10 ** 12))
        connection.putheader("X-Print-Dashboard", "1")
        connection.endheaders()
        self.assertEqual(connection.getresponse().status, 413)

    def test_bad_content_length(self):
        for value in ("abc", "-1", "1.5"):
            connection = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1])
            connection.putrequest("POST", "/api/print")
            connection.putheader("Content-Length", value)
            connection.putheader("X-Print-Dashboard", "1")
            connection.endheaders()
            self.assertEqual(connection.getresponse().status, 411, value)

    def test_client_that_vanishes_mid_upload_leaves_no_job(self):
        with socket.create_connection(self.server.server_address) as raw:
            raw.sendall(b"POST /api/print HTTP/1.1\r\nHost: x\r\nX-Print-Dashboard: 1\r\nContent-Length: 1000\r\n\r\n%PDF-1.4")
            raw.shutdown(socket.SHUT_WR)
            self.assertIn(b"400", raw.recv(100))
        self.assertEqual(self.fake.jobs, {})

    def test_cups_refusing_gives_a_message(self):
        self.fake.accepting = False
        self.assertEqual(self.upload(pdf()), (502, {"ok": False, "error": "The print queue didn't take the file."}))

    def test_cups_down_gives_a_message(self):
        self.fake.fail_all = True
        self.assertEqual(self.upload(pdf())[0], 502)

    def test_copies(self):
        for query, expected in (("", 1), ("?copies=5", 5), ("?copies=0", 1), ("?copies=9999", 20), ("?copies=abc", 1), ("?copies=-3", 1), ("?copies=1&copies=7", 1)):
            self.upload(pdf(), query=query)
            self.assertEqual(self.last_job()["copies"], expected, query)

    def test_colour_needs_the_toggle_on_and_the_job_to_ask(self):
        for allowed, query, expected in ((True, "?color=1", "color"), (True, "?color=0", "monochrome"), (True, "", "monochrome"),
                                         (False, "?color=1", "monochrome"), (False, "?color=0", "monochrome"), (True, "?color=true", "monochrome")):
            self.fake.color = allowed
            self.poll()
            self.upload(pdf(), query=query)
            self.assertEqual(self.last_job()["options"]["print-color-mode"], [expected], (allowed, query))

    def test_colour_is_refused_before_the_first_poll(self):
        fresh = Monitor(self.settings, self.cups, self.printer, self.history)
        server = create_server(self.settings, self.cups, fresh)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        threading.Thread(target=server.serve_forever, args=(0.01,), daemon=True).start()
        self.base = "http://127.0.0.1:%d" % server.server_address[1]
        self.upload(pdf(), query="?color=1")
        self.assertEqual(self.last_job()["options"]["print-color-mode"], ["monochrome"])

    def test_names(self):
        for raw, expected in (("%E0%A4%9A%E0%A4%BE.pdf", "चा.pdf"), ("..%2F..%2Fetc%2Fpasswd", "passwd"), ("", "Document"), ("<img src=x onerror=alert(1)>.pdf", "<img src=x onerror=alert(1)>.pdf")):
            self.upload(pdf(), raw)
            self.assertEqual(self.last_job()["name"], expected, raw)

    def test_missing_name_header(self):
        self.api("POST", "/api/print", pdf())
        self.assertEqual(self.last_job()["name"], "Document")

    def test_uploaded_job_appears_with_a_count_and_owner(self):
        self.upload(URF[:0] + png(), "photo.png", "?copies=3")
        self.settle()
        job = self.monitor.status()["jobs"][0]
        self.assertEqual((job["name"], job["pages"], job["user"]), ("photo.png", 3, "dashboard"))

    @unittest.skipUnless(HAS_PDFINFO, "pdfinfo not installed")
    def test_pdf_page_count_through_the_whole_path(self):
        self.upload(pdf(4), "doc.pdf", "?copies=2")
        self.settle()
        self.assertEqual(self.monitor.status()["jobs"][0]["pages"], 8)

    def test_ten_uploads_at_once_all_arrive(self):
        replies = []

        def send(i):
            replies.append(self.upload(pdf(), f"file{i}.pdf"))

        threads = [threading.Thread(target=send, args=(i,)) for i in range(10)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual([r[0] for r in replies], [200] * 10)
        self.assertEqual(sorted(j["name"] for j in self.fake.jobs.values()), sorted(f"file{i}.pdf" for i in range(10)))

    def test_status_stays_responsive_during_a_slow_upload(self):
        with socket.create_connection(self.server.server_address) as raw:
            raw.sendall(b"POST /api/print HTTP/1.1\r\nHost: x\r\nX-Print-Dashboard: 1\r\nContent-Length: 1000\r\n\r\n")
            self.assertEqual(self.api("GET", "/api/status")[0], 200)

    def test_files_are_not_kept_on_disk(self):
        self.upload(pdf(), "private-document.pdf")
        self.settle()
        for root, _, files in os.walk(self.tmp):
            for name in files:
                with open(os.path.join(root, name), "rb") as f:
                    self.assertNotIn(b"%PDF-", f.read())


class QueueOnlyTests(Case):
    """No PRINTER_IP: everything that comes from CUPS must still work."""

    def setUp(self):
        super().setUp()
        self.monitor.printer = Printer("")

    def test_status(self):
        self.poll()
        status = self.monitor.status()
        self.assertEqual((status["printer"]["online"], status["queue"]["online"], status["queue"]["model"]), (None, True, "Acme Inkjet 100"))

    def test_upload_and_track(self):
        self.upload(png(), "a.png")
        self.settle()
        self.assertEqual(self.monitor.status()["jobs"][0]["pages"], 1)


if __name__ == "__main__":
    unittest.main()
