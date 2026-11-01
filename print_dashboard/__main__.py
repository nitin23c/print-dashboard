"""Start the dashboard: python -m print_dashboard"""
import threading

from .config import Settings
from .cups import Cups
from .history import History
from .monitor import Monitor
from .printer import Printer
from .server import create_server


def main():
    settings = Settings.from_env()
    cups = Cups(settings)
    monitor = Monitor(settings, cups, Printer(settings.printer_ip), History(settings.db_path))
    threading.Thread(target=monitor.run, daemon=True).start()
    print(f"Listening on {settings.bind_address}:{settings.port}, queue {settings.queue} at {settings.cups_url}", flush=True)
    create_server(settings, cups, monitor).serve_forever()


if __name__ == "__main__":
    main()
