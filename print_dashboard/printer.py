"""Read the printer directly. Standard IPP status works on most network printers.
HP status pages add sleep state and page counters when the printer has them."""
import re
import urllib.request
import xml.etree.ElementTree as ET

from . import ipp

FIELDS = ("printer-state", "printer-state-reasons")
PROBLEMS = (("paper", "Out of paper", r"media-empty|media-needed|mediaempty|trayempty|outofpaper|paperout|loadpaper"),
            ("jam", "Paper jam", r"jam"),
            ("door", "Door or cover open", r"door|cover"),
            ("ink", "Ink system problem", r"marker-supply-empty|inkout|cartridgemissing"))


def find_problems(text):
    return [label for _, label, pattern in PROBLEMS if re.search(pattern, text.lower())]


def number(root, tag):
    value = root.findtext(f".//{tag}")
    return int(value) if value and value.isdigit() else None


class Printer:
    def __init__(self, ip, http_port=80, ipp_port=631):
        self.ip, self.http_port = ip, http_port
        self.client = ipp.Client(f"http://{ip}:{ipp_port}", "/ipp/print") if ip else None

    def hp_page(self, path):
        with urllib.request.urlopen(f"http://{self.ip}:{self.http_port}{path}", timeout=5) as reply:
            root = ET.fromstring(reply.read())
        for element in root.iter():
            element.tag = element.tag.split("}")[-1]
        return root

    def read_ipp(self):
        try:
            _, groups = self.client.call(ipp.GET_PRINTER, fields=FIELDS)
        except Exception:
            return None
        return next((g for g in groups if g["tag"] == ipp.PRINTER_GROUP), None)

    def read_hp(self):
        try:
            return self.hp_page("/DevMgmt/ProductStatusDyn.xml"), self.hp_page("/DevMgmt/ProductUsageDyn.xml")
        except Exception:
            return None

    def read(self):
        """online is None when no printer address is set: only the queue is watched."""
        if not self.client:
            return {"online": None, "problems": []}
        group, hp = self.read_ipp(), self.read_hp()
        if group is None and hp is None:
            return {"online": False, "problems": []}
        reasons = ipp.real_reasons(group or {})
        result = {"online": True, "sleeping": False, "notices": [], "state": ipp.first(group or {}, "printer-state")}
        categories = []
        if hp:
            status, usage = hp
            for item in status.findall("Status"):
                categories.append(item.findtext("StatusCategory") or "")
                if item.findtext("LocString"):
                    result["notices"].append(item.findtext("LocString"))
            result.update(sleeping="inPowerSave" in categories, sheets=number(usage, "SimplexSheets"),
                          mono=number(usage, "MonochromeImpressions"), color=number(usage, "ColorImpressions"),
                          jams=number(usage, "JamEvents"), misfeeds=number(usage, "MispickEvents"))
        result["problems"] = find_problems(" ".join(categories + reasons))
        result["notices"] = [n for n in result["notices"] if n not in result["problems"]]
        return result
