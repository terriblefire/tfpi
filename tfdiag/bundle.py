"""Zip a result directory for a bug report, with IP addresses and hostnames removed.

Included: report.html/txt, VCDs, histogram JSON/CSV, board/timing JSON (build
info, IDs, exerciser results), before/restore JSON, event logs (*.jsonl, *.log),
and the firmware read back from the board (firmware/cpld_readback.jed,
firmware/fpga_bitstream.bin).
"""

import re
import socket
import zipfile
from pathlib import Path

INCLUDE = ("*.html", "*.txt", "*.vcd", "*.svg", "*.json", "*.csv", "*.jsonl", "*.log",
           "cpld_readback.jed", "fpga_bitstream.bin")
IPV4 = re.compile(r"\b(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|1?\d?\d)\b")
IPV6 = re.compile(r"\b(?:[0-9a-fA-F]{1,4}:){7}[0-9a-fA-F]{1,4}\b"
                  r"|\b[0-9a-fA-F]{1,4}(?::[0-9a-fA-F]{1,4})*::(?:[0-9a-fA-F]{1,4}(?::[0-9a-fA-F]{1,4})*)?\b")
MDNS = re.compile(r"\b[A-Za-z0-9-]+\.(?:local|lan|home|internal)\b")
TEXT = {".html", ".txt", ".json", ".csv", ".jsonl", ".log", ".svg"}


def _hostnames():
    names = set()
    try:
        h = socket.gethostname()
        if h:
            names.add(h)
            names.add(h.split(".")[0])
        names.add(socket.getfqdn())
    except OSError:
        pass
    return {n for n in names if n and len(n) > 2 and n not in ("localhost",)}


def scrub(text, hostnames=None):
    text = IPV4.sub("<ip>", text)
    text = IPV6.sub("<ip6>", text)
    text = MDNS.sub("<host>", text)
    for h in sorted(hostnames if hostnames is not None else _hostnames(), key=len, reverse=True):
        text = re.sub(r"\b" + re.escape(h) + r"\b", "<host>", text)
    return text


def make(src, out, hostnames=None):
    src, out = Path(src), Path(out)
    hn = hostnames if hostnames is not None else _hostnames()
    files = []
    for pat in INCLUDE:
        files += [p for p in src.rglob(pat) if p.is_file() and p.resolve() != out.resolve()]
    files = sorted(set(files))
    contents = []
    out.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for p in files:
            rel = p.relative_to(src).as_posix()
            if p.suffix in TEXT:
                data = scrub(p.read_text(errors="replace"), hn).encode()
            else:
                data = p.read_bytes()
            z.writestr(rel, data)
            contents.append({"name": rel, "bytes": len(data)})
    return contents
