"""Identify print files and count their pages."""
import gzip
import re
import shutil
import struct
import subprocess
import tempfile

IMAGES = ("image/jpeg", "image/png")


def document_type(data):
    if b"%PDF-" in data[:1024]:
        return "application/pdf"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    return None


def count_pages(stream):
    """Count pages in a PDF, an image or the URF raster AirPrint sends. The file
    goes through a temporary file because pdfinfo needs one. None means unknown."""
    with tempfile.NamedTemporaryFile() as copy:
        shutil.copyfileobj(stream, copy)
        copy.flush()
        copy.seek(0)
        if copy.read(2) == b"\x1f\x8b":
            with tempfile.NamedTemporaryFile() as unpacked:
                with gzip.open(copy.name) as packed:
                    shutil.copyfileobj(packed, unpacked)
                unpacked.flush()
                return count_file(unpacked.name)
        return count_file(copy.name)


def count_file(path):
    with open(path, "rb") as f:
        head = f.read(1024)
    if head.startswith(b"UNIRAST") and len(head) >= 12:
        return struct.unpack(">I", head[8:12])[0] or None
    kind = document_type(head)
    if kind in IMAGES:
        return 1
    if kind != "application/pdf":
        return None
    try:
        output = subprocess.run(["pdfinfo", path], capture_output=True, text=True, timeout=20).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    found = re.search(r"^Pages:\s+(\d+)", output, re.M)
    return (int(found.group(1)) or None) if found else None
