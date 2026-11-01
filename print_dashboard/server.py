"""The web server: the page, the status API and the actions."""
import json
import os
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlsplit

from .pages import document_type

STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
FILES = {"/": ("index.html", "text/html; charset=utf-8"),
         "/app.js": ("app.js", "text/javascript; charset=utf-8"),
         "/style.css": ("style.css", "text/css; charset=utf-8")}
POLICY = "default-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"


def clean_name(raw):
    name = unquote(raw).replace("\\", "/").rsplit("/", 1)[-1]
    return re.sub(r"[\x00-\x1f\x7f]", "", name).strip() or "Document"


def is_number(value):
    return value.isascii() and value.isdigit()


def whole_number(values, default, limit):
    value = (values or [""])[0]
    return min(max(int(value), 1), limit) if is_number(value) and len(value) < 10 else default


def create_server(settings, cups, monitor):
    limit = settings.max_upload_mb * 1024 * 1024

    class Handler(BaseHTTPRequestHandler):
        # Drop a client that stops sending partway through an upload.
        timeout = 120

        def send(self, body, kind, code=200):
            self.send_response(code)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", POLICY)
            self.end_headers()
            self.wfile.write(body)

        def reply(self, code, **fields):
            self.send(json.dumps(fields).encode(), "application/json", code)

        def do_GET(self):
            path = urlsplit(self.path).path
            if path == "/api/status":
                self.reply(200, **monitor.status())
            elif path == "/healthz":
                self.reply(200, ok=True)
            elif path in FILES:
                name, kind = FILES[path]
                with open(os.path.join(STATIC, name), "rb") as f:
                    self.send(f.read(), kind)
            else:
                self.reply(404, ok=False, error="Not found.")

        def discard(self, size):
            try:
                while size > 0:
                    chunk = self.rfile.read(min(size, 1024 * 1024))
                    if not chunk:
                        break
                    size -= len(chunk)
            except OSError:
                self.close_connection = True

        def upload(self, query):
            length = self.headers.get("Content-Length", "")
            size = int(length) if is_number(length) else 0
            if not size:
                self.reply(411, ok=False, error="The upload had no size.")
                return
            if size > limit:
                # Read a slightly large file first: closing on a client that is
                # still sending would lose this message.
                if size <= 2 * limit:
                    self.discard(size)
                self.reply(413, ok=False, error=f"Files can be up to {settings.max_upload_mb} MB.")
                return
            try:
                data = self.rfile.read(size)
            except OSError:
                self.close_connection = True
                return
            kind = document_type(data)
            if len(data) < size:
                self.reply(400, ok=False, error="The upload was cut off.")
            elif not kind:
                self.reply(415, ok=False, error="Only PDF, JPEG and PNG files can be printed.")
            else:
                name = clean_name(self.headers.get("X-File-Name", ""))
                copies = whole_number(query.get("copies"), 1, settings.max_copies)
                color = monitor.color_allowed() and query.get("color") == ["1"]
                self.act("print", lambda: cups.submit(name, data, kind, copies, color),
                         "The print queue didn't take the file.")

        def act(self, label, action, failure=None):
            try:
                done = action()
            except Exception as error:
                print(f"{label} failed:", type(error).__name__, flush=True)
                done = False
            self.reply(200 if done else 502, ok=done, **({} if done or not failure else {"error": failure}))

        def do_POST(self):
            url = urlsplit(self.path)
            cancelling = re.fullmatch(r"/api/cancel/(\d{1,9})", url.path)
            coloring = re.fullmatch(r"/api/color/(on|off)", url.path)
            uploading = url.path == "/api/print"
            # The custom header blocks cross-site form posts from other pages.
            if self.headers.get("X-Print-Dashboard") != "1":
                self.reply(403, ok=False, error="Missing X-Print-Dashboard header.")
            elif uploading:
                self.upload(parse_qs(url.query))
            elif cancelling:
                self.act("cancel", lambda: cups.cancel(int(cancelling.group(1))))
            elif coloring and monitor.busy():
                self.reply(409, ok=False, error="busy")
            elif coloring:
                self.act("colour change", lambda: cups.set_color(coloring.group(1) == "on"))
            else:
                self.reply(404, ok=False, error="Not found.")

        def log_message(self, *args):
            pass

    return ThreadingHTTPServer((settings.bind_address, settings.port), Handler)
