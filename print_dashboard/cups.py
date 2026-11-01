"""Everything the dashboard asks of CUPS. It all goes over the network, so CUPS
can run in another container or on another machine."""
import os
import urllib.error
import urllib.parse
import urllib.request

from . import ipp
from .pages import IMAGES, count_pages
from .ppd import gray_only, job_chooses

JOB_FIELDS = ("job-id", "job-name", "job-state", "job-state-reasons", "job-originating-user-name",
              "job-k-octets", "time-at-creation", "time-at-processing", "time-at-completed",
              "job-impressions-completed", "job-media-sheets-completed", "copies")
QUEUE_FIELDS = ("printer-state", "printer-state-reasons", "printer-is-accepting-jobs", "color-supported",
                "printer-make-and-model")
JOB_STATES = {3: "waiting", 4: "held", 5: "printing", 6: "paused", 7: "cancelled", 8: "failed", 9: "completed"}
STOPPED = 5


class Cups:
    def __init__(self, settings):
        self.settings = settings
        path = "/printers/" + urllib.parse.quote(settings.queue)
        self.client = ipp.Client(settings.cups_url, path, settings.cups_user, settings.cups_password)
        self.color_ppd = os.path.join(settings.data_dir, "color.ppd")

    def jobs(self, which):
        # The queue hides job names and owners from anyone but an admin.
        _, groups = self.client.call(ipp.GET_JOBS, fields=JOB_FIELDS, which=which, auth=True)
        jobs = []
        for group in groups:
            if group["tag"] != ipp.JOB_GROUP or "job-id" not in group:
                continue
            first = lambda name, default=None: ipp.first(group, name, default)
            jobs.append({"id": first("job-id"), "name": str(first("job-name", "Document")).strip("‎‏ "),
                         "user": first("job-originating-user-name", ""), "size_kb": first("job-k-octets", 0),
                         "created": first("time-at-creation", 0), "started": first("time-at-processing", 0),
                         "finished": first("time-at-completed", 0), "copies": first("copies", 1),
                         "done": first("job-impressions-completed", 0), "reasons": group.get("job-state-reasons", []),
                         "state": JOB_STATES.get(first("job-state"), "unknown")})
        return jobs

    def login_ok(self):
        """CUPS answers status requests without checking the password, and just
        hides job names. So ask for something that really needs the login."""
        check = urllib.request.Request(self.client.url + "/admin/conf/cupsd.conf", method="HEAD",
                                       headers={"Authorization": self.client.login()})
        try:
            with urllib.request.urlopen(check, timeout=self.client.timeout):
                return True
        except urllib.error.HTTPError as error:
            return error.code != 401

    def queue(self):
        _, groups = self.client.call(ipp.GET_PRINTER, fields=QUEUE_FIELDS)
        group = next((g for g in groups if g["tag"] == ipp.PRINTER_GROUP), None)
        if group is None:
            raise LookupError(f"queue {self.settings.queue} not found")
        return {"online": True, "accepting": ipp.first(group, "printer-is-accepting-jobs", False),
                "color": ipp.first(group, "color-supported", False), "model": ipp.first(group, "printer-make-and-model", ""),
                "stopped": ipp.first(group, "printer-state") == STOPPED, "reasons": ipp.real_reasons(group)}

    def job_pages(self, job_id):
        """Count a queued job's pages by fetching its file from CUPS."""
        with self.client.open(ipp.GET_DOCUMENT, job_id=job_id, extra=ipp.integer("document-number", 1), auth=True, timeout=60) as reply:
            code, _ = ipp.parse(reply)
            return count_pages(reply) if code < 0x0100 else None

    def cancel(self, job_id):
        code, _ = self.client.call(ipp.CANCEL_JOB, job_id=job_id, auth=True)
        return code < 0x0100

    def submit(self, name, data, kind, copies, color):
        job = ipp.integer("copies", copies) + ipp.attr(ipp.KEYWORD, "print-color-mode", "color" if color else "monochrome")
        if kind in IMAGES:
            job += ipp.attr(ipp.BOOLEAN, "fit-to-page", b"\x01")
        extra = ipp.attr(ipp.NAME, "job-name", name.encode()[:255].decode(errors="ignore")) + ipp.attr(ipp.MIME_TYPE, "document-format", kind)
        code, _ = self.client.call(ipp.PRINT_JOB, data=data, extra=extra, job=job, timeout=120)
        return code < 0x0100

    def set_color(self, enabled):
        client = self.client
        with urllib.request.urlopen(client.url + client.path + ".ppd", timeout=client.timeout) as reply:
            current = reply.read().decode()
        if "*ColorDevice: True" in current:
            with open(self.color_ppd, "w") as f:
                f.write(current)
        with open(self.color_ppd) as f:
            color = f.read()
        ppd = job_chooses(color) if enabled else gray_only(color)
        code, _ = client.call(ipp.MODIFY_PRINTER, path="/admin/", auth=True, data=ppd.encode())
        return code < 0x0100
