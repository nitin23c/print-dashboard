"""Settings, read from environment variables."""
import os
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Settings:
    cups_url: str = "http://127.0.0.1:631"
    queue: str = "Home_Printer"
    cups_user: str = "cupsadmin"
    # Kept out of repr() so settings can't reach a log by accident.
    cups_password: str = field(default="cupsadmin", repr=False)
    printer_ip: str = ""
    db_path: str = "/data/history.db"
    bind_address: str = "0.0.0.0"
    port: int = 8080
    title: str = "Home Printer"
    note: str = ""
    max_upload_mb: int = 100
    max_copies: int = 20
    keep_days: int = 90

    @property
    def data_dir(self):
        return os.path.dirname(self.db_path)

    @classmethod
    def from_env(cls, env=os.environ):
        """CUPSADMIN and CUPSPASSWORD have the same names and defaults as in the
        CUPS image, so both containers can read one .env file."""
        defaults = cls()
        admin = env.get("CUPSADMIN") or defaults.cups_user

        def number(name, default):
            value = env.get(name, "")
            if not value:
                return default
            if not (value.isascii() and value.isdigit()) or int(value) < 1:
                raise ValueError(f"{name} must be a whole number above 0")
            return int(value)

        return cls(cups_url=(env.get("CUPS_URL") or defaults.cups_url).rstrip("/"),
                   queue=env.get("CUPS_QUEUE") or defaults.queue,
                   cups_user=admin,
                   cups_password=env.get("CUPSPASSWORD") or admin,
                   printer_ip=env.get("PRINTER_IP", ""),
                   db_path=env.get("DB_PATH") or defaults.db_path,
                   bind_address=env.get("BIND_ADDRESS") or defaults.bind_address,
                   port=number("PORT", defaults.port),
                   title=env.get("DASHBOARD_TITLE") or defaults.title,
                   note=env.get("DASHBOARD_NOTE", ""),
                   max_upload_mb=number("MAX_UPLOAD_MB", defaults.max_upload_mb),
                   max_copies=number("MAX_COPIES", defaults.max_copies),
                   keep_days=number("KEEP_DAYS", defaults.keep_days))
