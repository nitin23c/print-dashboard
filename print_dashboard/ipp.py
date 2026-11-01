"""A small IPP client, covering only the operations the dashboard needs."""
import base64
import io
import struct
import urllib.parse
import urllib.request

PRINT_JOB, CANCEL_JOB, GET_JOBS, GET_PRINTER = 0x0002, 0x0008, 0x000A, 0x000B
MODIFY_PRINTER, GET_DOCUMENT = 0x4003, 0x4027

INTEGER, BOOLEAN, ENUM, NAME, CHARSET, LANGUAGE, URI, KEYWORD, MIME_TYPE = 0x21, 0x22, 0x23, 0x42, 0x47, 0x48, 0x45, 0x44, 0x49
OPERATION_GROUP, JOB_GROUP, END, PRINTER_GROUP = 0x01, 0x02, 0x03, 0x04


class IppError(Exception):
    """The reply wasn't valid IPP."""


def attr(tag, name, value):
    name, value = name.encode(), value if isinstance(value, bytes) else value.encode()
    return struct.pack(">BH", tag, len(name)) + name + struct.pack(">H", len(value)) + value


def integer(name, value):
    return attr(INTEGER, name, struct.pack(">i", value))


def encode(operation, uri, user, fields=(), which=None, job_id=None, extra=b"", job=b""):
    body = b"\x02\x00" + struct.pack(">HI", operation, 1) + bytes([OPERATION_GROUP])
    body += attr(CHARSET, "attributes-charset", "utf-8") + attr(LANGUAGE, "attributes-natural-language", "en")
    body += attr(URI, "printer-uri", uri)
    if job_id is not None:
        body += integer("job-id", job_id)
    body += attr(NAME, "requesting-user-name", user)
    if which:
        body += attr(KEYWORD, "which-jobs", which)
    for i, field in enumerate(fields):
        body += attr(KEYWORD, "" if i else "requested-attributes", field)
    return body + extra + (bytes([JOB_GROUP]) + job if job else b"") + bytes([END])


def parse(stream):
    """Read the IPP part of a reply from a stream. Any document that follows
    stays unread, so large files don't have to fit in memory."""

    def take(size):
        data = stream.read(size)
        if len(data) < size:
            raise IppError("reply ended early")
        return data

    code = struct.unpack(">H", take(8)[2:4])[0]
    groups, group, name = [], None, None
    while True:
        tag = take(1)[0]
        if tag == END:
            return code, groups
        if tag < 0x10:
            group = {"tag": tag}
            groups.append(group)
            continue
        if group is None:
            raise IppError("attribute outside a group")
        size = struct.unpack(">H", take(2))[0]
        if size:
            name = take(size).decode(errors="replace")
            group.setdefault(name, [])
        size = struct.unpack(">H", take(2))[0]
        raw = take(size)
        if name is None:
            continue
        if tag in (INTEGER, ENUM) and size == 4:
            group[name].append(struct.unpack(">i", raw)[0])
        elif tag == BOOLEAN and size == 1:
            group[name].append(bool(raw[0]))
        elif 0x35 <= tag <= 0x4A:
            group[name].append(raw.decode(errors="replace"))


def parse_bytes(data):
    return parse(io.BytesIO(data))


def first(group, name, default=None):
    return (group.get(name) or [default])[0]


def real_reasons(group):
    """Drop reasons ending in -report: printers leave them set when all is well."""
    return [r for r in group.get("printer-state-reasons", []) if r != "none" and not r.endswith("-report")]


class Client:
    def __init__(self, url, path, user="", password="", timeout=8):
        self.url, self.path, self.user, self.password, self.timeout = url, path, user, password, timeout
        self.uri = "ipp://" + urllib.parse.urlsplit(url).netloc + path

    def open(self, operation, path=None, auth=False, data=b"", timeout=None, **request):
        """Send a request and return the open HTTP reply."""
        body = encode(operation, self.uri, self.user if auth else "dashboard", **request) + data
        call = urllib.request.Request(self.url + (path or self.path), body, {"Content-Type": "application/ipp"})
        if auth:
            call.add_header("Authorization", self.login())
        return urllib.request.urlopen(call, timeout=timeout or self.timeout)

    def login(self):
        return "Basic " + base64.b64encode(f"{self.user}:{self.password}".encode()).decode()

    def call(self, operation, **request):
        with self.open(operation, **request) as reply:
            return parse(reply)
