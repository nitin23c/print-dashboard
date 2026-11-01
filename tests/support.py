"""Test helpers: a fake CUPS and printer that speak real IPP over HTTP, so tests
exercise the same code paths as production without a printer or any paper."""
import base64
import io
import json
import os
import shutil
import struct
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from print_dashboard import ipp
from print_dashboard.config import Settings
from print_dashboard.cups import Cups
from print_dashboard.history import History
from print_dashboard.monitor import Monitor
from print_dashboard.printer import Printer
from print_dashboard.server import create_server

ADMIN, PASSWORD = "admin", "s3cret-test-value"

PPD = """*PPD-Adobe: "4.3"
*ColorDevice: True
*DefaultColorSpace: RGB
*OpenUI *ColorModel/Output Mode: PickOne
*DefaultColorModel: RGB
*ColorModel RGB/Color: "<</cupsColorSpace 1>>setpagedevice"
*ColorModel Gray/Grayscale: "<</cupsColorSpace 18>>setpagedevice"
*en_US.ColorModel RGB/Color: ""
*CloseUI: *ColorModel
*APPrinterPreset Color/Colour: "*ColorModel RGB"
*End
*APPrinterPreset Gray/Grey: "*ColorModel Gray"
*End
"""

HP_STATUS = """<?xml version="1.0"?><psdyn:ProductStatusDyn xmlns:psdyn="http://example.test/psdyn" xmlns:pscat="http://example.test/pscat">
<psdyn:Status><pscat:StatusCategory>{category}</pscat:StatusCategory>{notice}</psdyn:Status></psdyn:ProductStatusDyn>"""

HP_USAGE = """<?xml version="1.0"?><pudyn:ProductUsageDyn xmlns:pudyn="http://example.test/pudyn" xmlns:dd="http://example.test/dd">
<dd:SimplexSheets>{sheets}</dd:SimplexSheets><dd:MonochromeImpressions>900</dd:MonochromeImpressions>
<dd:ColorImpressions>100</dd:ColorImpressions><dd:JamEvents>1</dd:JamEvents><dd:MispickEvents>2</dd:MispickEvents></pudyn:ProductUsageDyn>"""


def pdf(pages=3):
    objects = [b"<</Type/Catalog/Pages 2 0 R>>",
               b"<</Type/Pages/Kids[" + b" ".join(b"%d 0 R" % (i + 3) for i in range(pages)) + b"]/Count %d>>" % pages]
    objects += [b"<</Type/Page/Parent 2 0 R/MediaBox[0 0 595 842]>>"] * pages
    out, offsets = b"%PDF-1.4\n", []
    for i, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + body + b"\nendobj\n"
    start = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    out += b"".join(b"%010d 00000 n \n" % o for o in offsets)
    return out + b"trailer<</Size %d/Root 1 0 R>>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, start)


def png():
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    header = chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
    return b"\x89PNG\r\n\x1a\n" + header + chunk(b"IDAT", zlib.compress(b"\0\xff\0\0")) + chunk(b"IEND", b"")


JPEG = b"\xff\xd8\xff\xe0" + b"\0" * 64
URF = b"UNIRAST\0" + struct.pack(">I", 7) + b"\0" * 64


def encode_value(name, value):
    values = value if isinstance(value, list) else [value]
    out = b""
    for i, item in enumerate(values):
        label = "" if i else name
        if isinstance(item, bool):
            out += ipp.attr(ipp.BOOLEAN, label, b"\x01" if item else b"\x00")
        elif isinstance(item, int):
            out += ipp.attr(ipp.ENUM if name in ("job-state", "printer-state") else ipp.INTEGER, label, struct.pack(">i", item))
        else:
            out += ipp.attr(ipp.KEYWORD, label, str(item))
    return out


def encode_reply(code, groups=()):
    out = b"\x02\x00" + struct.pack(">HI", code, 1) + bytes([ipp.OPERATION_GROUP])
    out += ipp.attr(ipp.CHARSET, "attributes-charset", "utf-8") + ipp.attr(ipp.LANGUAGE, "attributes-natural-language", "en")
    for tag, fields in groups:
        out += bytes([tag]) + b"".join(encode_value(k, v) for k, v in fields.items())
    return out + bytes([ipp.END])


class Fake:
    """One server playing both CUPS and the network printer."""

    def __init__(self, queue="Test_Queue"):
        self.queue = queue
        self.jobs, self.requests, self.next_id = {}, [], 1
        self.ppd = gray_ppd = PPD
        self.color, self.accepting, self.state = True, True, 3
        self.reasons = ["none"]
        self.model = "Acme Inkjet 100"
        self.hp = True
        self.category, self.notice, self.sheets = "ready", "", 500
        self.printer_ipp, self.printer_reasons = True, ["none"]
        self.fail_all, self.drop_document, self.lock = False, False, threading.Lock()
        fake = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def out(self, body, kind="application/ipp", code=200):
                self.send_response(code)
                self.send_header("Content-Type", kind)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                if self.path == f"/printers/{fake.queue}.ppd":
                    self.out(fake.ppd.encode(), "text/plain")
                elif self.path == "/DevMgmt/ProductStatusDyn.xml" and fake.hp:
                    notice = f"<LocString>{fake.notice}</LocString>" if fake.notice else ""
                    self.out(HP_STATUS.format(category=fake.category, notice=notice).encode(), "text/xml")
                elif self.path == "/DevMgmt/ProductUsageDyn.xml" and fake.hp:
                    self.out(HP_USAGE.format(sheets=fake.sheets).encode(), "text/xml")
                else:
                    self.out(b"", "text/plain", 404)

            def admin(self):
                expected = "Basic " + base64.b64encode(f"{ADMIN}:{PASSWORD}".encode()).decode()
                return self.headers.get("Authorization") == expected

            def do_HEAD(self):
                self.send_response(200 if self.admin() else 401)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"]))
                if fake.fail_all:
                    self.out(b"", "text/plain", 500)
                    return
                # Like real CUPS: status requests pass with a wrong password,
                # and only the operations that need a login reject it.
                needs_login = struct.unpack(">H", body[2:4])[0] in (ipp.CANCEL_JOB, ipp.GET_DOCUMENT, ipp.MODIFY_PRINTER)
                if needs_login and not self.admin():
                    self.out(b"", "text/plain", 401)
                    return
                self.out(fake.handle(self.path, body, self.admin()))

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}"
        threading.Thread(target=self.server.serve_forever, args=(0.01,), daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    def add_job(self, name="doc.pdf", state=3, data=None, copies=1, user="mobile", done=0):
        with self.lock:
            job_id, self.next_id = self.next_id, self.next_id + 1
            now = int(time.time())
            self.jobs[job_id] = dict(id=job_id, name=name, state=state, data=pdf(1) if data is None else data, copies=copies,
                                     user=user, created=now, finished=now if state >= 7 else 0, done=done, options={})
            return job_id

    def handle(self, path, body, admin):
        stream = io.BytesIO(body)
        operation = struct.unpack(">H", body[2:4])[0]
        _, groups = ipp.parse(stream)
        attrs = next(g for g in groups if g["tag"] == ipp.OPERATION_GROUP)
        options = next((g for g in groups if g["tag"] == ipp.JOB_GROUP), {})
        self.requests.append(dict(path=path, operation=operation, attrs=attrs, options=options, admin=admin))
        if path == "/ipp/print":
            if not self.printer_ipp:
                return encode_reply(0x0500)
            return encode_reply(0, [(ipp.PRINTER_GROUP, {"printer-state": self.state, "printer-state-reasons": self.printer_reasons})])
        if operation == ipp.GET_PRINTER:
            if path != f"/printers/{self.queue}":
                return encode_reply(0x0406)
            return encode_reply(0, [(ipp.PRINTER_GROUP, {"printer-state": self.state, "printer-state-reasons": self.reasons,
                                                         "printer-is-accepting-jobs": self.accepting, "color-supported": self.color,
                                                         "printer-make-and-model": self.model})])
        if operation == ipp.GET_JOBS:
            which = range(7, 10) if attrs["which-jobs"] == ["completed"] else range(3, 7)
            return encode_reply(0, [(ipp.JOB_GROUP, self.job_fields(j, admin)) for j in self.jobs.values() if j["state"] in which])
        if operation == ipp.CANCEL_JOB:
            job = self.jobs.get(attrs["job-id"][0])
            if not job:
                return encode_reply(0x0406)
            job.update(state=7, finished=int(time.time()))
            return encode_reply(0)
        if operation == ipp.GET_DOCUMENT:
            job = self.jobs.get(attrs["job-id"][0])
            if not admin or not job or self.drop_document:
                return encode_reply(0x0406)
            return encode_reply(0) + job["data"]
        if operation == ipp.PRINT_JOB:
            if not self.accepting:
                return encode_reply(0x0506)
            job_id = self.add_job(attrs["job-name"][0], data=stream.read(), copies=options.get("copies", [1])[0], user=attrs["requesting-user-name"][0])
            self.jobs[job_id].update(options=options, format=attrs["document-format"][0])
            return encode_reply(0, [(ipp.JOB_GROUP, {"job-id": job_id})])
        if operation == ipp.MODIFY_PRINTER:
            if path != "/admin/" or not admin:
                return encode_reply(0x0403)
            self.ppd = stream.read().decode()
            self.color = "*ColorDevice: True" in self.ppd
            return encode_reply(0)
        return encode_reply(0x0501)

    @staticmethod
    def job_fields(job, admin):
        fields = {"job-id": job["id"], "job-state": job["state"], "job-state-reasons": "none", "job-k-octets": len(job["data"]) // 1024 + 1,
                  "time-at-creation": job["created"], "time-at-completed": job["finished"], "copies": job["copies"],
                  "job-impressions-completed": job["done"]}
        # Like real CUPS: names are only shown to an admin.
        return {**fields, "job-name": job["name"], "job-originating-user-name": job["user"]} if admin else fields


class Case(unittest.TestCase):
    """Starts a fake CUPS and a real dashboard server for each test."""
    queue = "Test_Queue"

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.fake = Fake(self.queue)
        self.addCleanup(self.fake.close)
        self.settings = self.make_settings()
        self.cups = Cups(self.settings)
        self.printer = Printer("127.0.0.1", self.fake.port, self.fake.port)
        self.history = History(self.settings.db_path)
        self.monitor = Monitor(self.settings, self.cups, self.printer, self.history)
        self.server = create_server(self.settings, self.cups, self.monitor)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.base = "http://127.0.0.1:%d" % self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, args=(0.01,), daemon=True).start()

    def make_settings(self, **overrides):
        values = dict(cups_url=self.fake.url, queue=self.queue, cups_user=ADMIN, cups_password=PASSWORD,
                      db_path=os.path.join(self.tmp, "history.db"), bind_address="127.0.0.1", port=0, title="Test Printer")
        return Settings(**{**values, **overrides})

    def poll(self):
        self.monitor.next_printer = 0
        self.monitor.poll()

    def settle(self, seconds=20):
        """Poll until page-count threads finish."""
        end = time.time() + seconds
        while time.time() < end:
            self.poll()
            if not any(e.get("counting") for e in self.monitor.tracked.values()):
                self.poll()
                return
            time.sleep(0.02)
        self.fail("page count didn't finish")

    def request(self, method, path, body=None, headers=None):
        headers = {"X-Print-Dashboard": "1", **(headers or {})}
        call = urllib.request.Request(self.base + path, body, headers, method=method)
        try:
            with urllib.request.urlopen(call, timeout=10) as reply:
                return reply.status, reply.read(), reply.headers
        except urllib.error.HTTPError as error:
            return error.code, error.read(), error.headers

    def api(self, method, path, body=None, headers=None):
        code, data, _ = self.request(method, path, body, headers)
        return code, json.loads(data) if data else None

    def upload(self, data, name="file.pdf", query=""):
        return self.api("POST", "/api/print" + query, data, {"X-File-Name": name})

    def last_job(self):
        return self.fake.jobs[max(self.fake.jobs)]
