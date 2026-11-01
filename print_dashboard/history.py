"""Finished jobs, kept in SQLite. Stores job names and counts, never document contents."""
import sqlite3
import time
from contextlib import closing

COLUMNS = "job_id, created, name, user, size_kb, state, pages, finished"


class History:
    def __init__(self, path, keep_days=90):
        self.path, self.keep_days = path, keep_days
        with closing(self.connect()) as db, db:
            db.execute("""CREATE TABLE IF NOT EXISTS jobs (job_id INTEGER, created INTEGER, name TEXT, user TEXT,
                          size_kb INTEGER, state TEXT, pages INTEGER, finished INTEGER, PRIMARY KEY (job_id, created))""")

    def connect(self):
        db = sqlite3.connect(self.path)
        db.row_factory = sqlite3.Row
        return db

    def save(self, job, pages):
        with closing(self.connect()) as db, db:
            db.execute(f"INSERT OR IGNORE INTO jobs ({COLUMNS}) VALUES (?,?,?,?,?,?,?,?)",
                       (job["id"], job["created"], job["name"], job["user"], job["size_kb"], job["state"], pages, job["finished"]))
            db.execute("DELETE FROM jobs WHERE finished < ?", (time.time() - self.keep_days * 86400,))

    def recent(self, limit=25):
        with closing(self.connect()) as db:
            return [dict(r) for r in db.execute("SELECT * FROM jobs ORDER BY finished DESC LIMIT ?", (limit,))]

    def totals(self, now=None):
        now = time.localtime(now)
        midnight = time.mktime(now[:3] + (0, 0, 0, 0, 0, -1))
        month = time.mktime(now[:2] + (1, 0, 0, 0, 0, 0, -1))
        query = "SELECT COUNT(*), COALESCE(SUM(pages),0) FROM jobs WHERE state='completed' AND finished>=?"
        with closing(self.connect()) as db:
            found = {name: db.execute(query, (since,)).fetchone() for name, since in (("today", midnight), ("month", month))}
        return {name: {"jobs": row[0], "pages": row[1]} for name, row in found.items()}
