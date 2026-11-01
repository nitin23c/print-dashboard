"""Poll CUPS and the printer, and keep the state the page shows."""
import json
import re
import threading
import time
import urllib.error

from .printer import PROBLEMS

MAX_COUNT_TRIES = 3


class Monitor:
    def __init__(self, settings, cups, printer, history, interval=2):
        self.settings, self.cups, self.printer, self.history, self.interval = settings, cups, printer, history, interval
        self.lock = threading.Lock()
        self.live = {"printer": {"online": None}, "queue": {}, "jobs": []}
        self.tracked = {}
        self.sheets = None
        self.next_printer = 0
        self.login = True

    def count(self, job_id, entry):
        try:
            entry["total"] = self.cups.job_pages(job_id)
        except Exception as error:
            print("page count failed:", type(error).__name__, flush=True)
        entry["counting"] = False

    def track(self, job):
        """Add page totals and progress to a job that is waiting or printing."""
        entry = self.tracked.setdefault((job["id"], job["created"]), {"baseline": self.sheets, "total": None, "printed": 0, "tries": 0})
        if entry["total"] is None and not entry.get("counting") and entry["tries"] < MAX_COUNT_TRIES:
            # Fetching a file can take a while, so it runs beside the poll.
            entry["counting"] = True
            entry["tries"] += 1
            threading.Thread(target=self.count, args=(job["id"], entry), daemon=True).start()
        if job["state"] == "printing" and entry.get("started") is None:
            entry["started"], entry["baseline"] = True, self.sheets
        if entry.get("started") and self.sheets is not None and entry["baseline"] is not None:
            entry["printed"] = max(entry["printed"], self.sheets - entry["baseline"])
        entry["printed"] = max(entry["printed"], job["done"])
        total = entry["total"] * job["copies"] if entry["total"] else None
        job.update(pages=total, printed=entry["printed"])

    def save(self, job):
        entry = self.tracked.pop((job["id"], job["created"]), {})
        total = entry["total"] * job["copies"] if entry.get("total") else None
        pages = total if job["state"] == "completed" else None
        job = {**job, "finished": job["finished"] or time.time()}
        self.history.save(job, pages or job["done"] or entry.get("printed") or None)

    def poll(self):
        try:
            jobs = self.cups.jobs("not-completed")
            if time.time() >= self.next_printer:
                printer = self.printer.read()
                self.sheets = printer.get("sheets", self.sheets)
                self.login = self.cups.login_ok()
                with self.lock:
                    self.live["printer"] = printer
                self.next_printer = time.time() + (4 if jobs else 30)
            for job in jobs:
                self.track(job)
            for job in self.cups.jobs("completed"):
                self.save(job)
            queue = {**self.cups.queue(), "login": self.login}
            with self.lock:
                self.live.update(jobs=jobs, queue=queue, updated=time.time())
        except urllib.error.HTTPError as error:
            print("poll failed: HTTP", error.code, flush=True)
            with self.lock:
                self.live["queue"] = {"online": False, "error": "login" if error.code in (401, 403) else "http"}
        except Exception as error:
            print("poll failed:", type(error).__name__, flush=True)
            with self.lock:
                self.live["queue"] = {"online": False, "error": "unreachable"}

    def run(self):
        while True:
            self.poll()
            time.sleep(self.interval)

    def busy(self):
        with self.lock:
            return bool(self.live["jobs"])

    def color_allowed(self):
        with self.lock:
            return self.live["queue"].get("color", False)

    def status(self):
        with self.lock:
            snapshot = json.loads(json.dumps(self.live))
        printer, queue = snapshot["printer"], snapshot["queue"]
        problems = list(printer.get("problems", []))
        if snapshot["jobs"]:
            # Queue reasons can go stale, so they only count while a job is running.
            searched = " ".join(queue.get("reasons", [])).lower()
            problems += [label for _, label, pattern in PROBLEMS if re.search(pattern, searched) and label not in problems]
        settings = self.settings
        snapshot.update(problems=problems, history=self.history.recent(), totals=self.history.totals(),
                        title=settings.title, note=settings.note,
                        limits={"upload_mb": settings.max_upload_mb, "copies": settings.max_copies})
        return snapshot
