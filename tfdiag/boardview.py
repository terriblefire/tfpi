"""Board view: a stylised TF4060 r5 board (inline SVG) with the findings on it.

    html_section(data)  -> HTML block for report.html ("" when there is nothing to show)
    findings(data)      -> [{id, kind, title, detail, where, nets, pads, parts, pairs, links, colours,
                             source, focus}]
                           nets: board nets (first = main colour, then the others; colours: net ->
                           colour index); pads: [part, pad] key pins (ringed); pairs: [part, pad_a,
                           pad_b] suspect neighbours; links: [part, pad, part, pad] an open between
                           two pins; focus: zoom to the key pads ("pads") or the whole nets ("nets")
    svg(board)          -> the board SVG
    page(data)          -> a whole HTML page (python3 -m tfdiag.boardview --dir DIR / --demo FILE)

`data` is what report.collect() returns: {"board": board.json, "timing": timing.json, ...}.
Geometry is data/r5_board.json (scripts/gen_r5_board.py in tf4060v2, from the Eagle board),
nets and CPLD/FPGA pins come from data/r5_netlist.json. Findings stay observations, as in the
rest of the report: this only shows where on the board they are. A finding this module does
not understand is listed without a board location.

The page part is vanilla JS + CSS inside the block (no CDN): it works offline in a saved
report.html and inline in a tfpi page (each block is scoped by its own id). Click a row to
show it on the board; click a pad to show its net; wheel/drag to zoom/pan; Top/Bottom flips
the board (bottom parts are drawn faint and dashed from the other side).
"""

import argparse
import hashlib
import html
import itertools
import json
import math
import re
import sys
from functools import lru_cache
from pathlib import Path

from . import netlist as NL

DEFAULT = Path(__file__).resolve().parent / "data" / "r5_board.json"
CPLD, FPGA, CPU, SLOT = "BUSCPLD", "RAMFPGA", "IC1", "CN100"
LABELS = {
    "BUSCPLD": "XC95288XL bus CPLD", "RAMFPGA": "iCE40HX4K RAM FPGA", "IC1": "68060",
    "IC2": "SDRAM (bottom)", "IC3": "SDRAM (bottom)", "IC7": "SDRAM", "IC8": "SDRAM",
    "IC4": "VHC245", "IC5": "VHC245", "IC6": "VHC245",
    "IC9": "LVC1G07 reset", "IC10": "AUP1G79 reset", "IC11": "clock buffer", "OSC2": "25 MHz osc",
    "CN100": "Amiga CPU slot", "PI_HEADER": "Pi header", "JTAG1": "CPLD JTAG", "CPUJTAG": "060 JTAG",
    "U9": "SPI flash", "U8": "1.2 V", "PWR": "LM317", "IDE": "IDE",
    **{f"U{i}": "bus switch" for i in range(1, 8)},
}
JTAG_NETS = ("TCK", "TMS", "TDI", "TDO")
SAMPLE_CLOCKS = ("BCLK", "PCLK")            # UCF names that toggle while the normal image runs
# timing clocks (clocks.py names) -> (nets, key pads)
CLOCK_GEOMETRY = {
    "fabric": (["CLK25M"], [("OSC2", "3"), (FPGA, "129")]),
    "CLKAUX": (["CLK25M"], [("OSC2", "3"), (FPGA, "134")]),
    "pll_ratio": (["CLK25M"], [(FPGA, "129"), (FPGA, "134")]),
}
SUPPLY = re.compile(r"^(GND|VCC\w*|3V3\w*|\+?\d+V\d*)$", re.I)
ICS = re.compile(r"^(IC|U|BUSCPLD|RAMFPGA|CN|PI_HEADER|JTAG|CPUJTAG|OSC|IDE)")
RATS_MAX = 60                                 # no ratsnest for bigger (supply) nets
_COUNTER = itertools.count(1)


@lru_cache(maxsize=4)
def _load(path):
    return json.loads(Path(path).read_text())


def load_board(path=None):
    return _load(str(path or DEFAULT))


# ---- findings -> geometry --------------------------------------------------------------------

def resolve_net(nl, name):
    """Board net for a UCF/PCF/board name ('DD<3>', 'A[0]', 'TS'), or None."""
    if not name:
        return None
    name = str(name)
    if name in nl.nets:
        return name
    pin = nl.cpld_pin_by_name(name)
    if pin:
        return nl.cpld_pins[pin].get("net")
    for e in nl.fpga_pins.values():
        if e.get("pcf") == name:
            return e.get("net")
    m = re.match(r"^(\w+?)[<\[(](\d+)[>\])]$", name)
    if m and m.group(1) + m.group(2) in nl.nets:
        return m.group(1) + m.group(2)
    return None


def _net_parts(nl, net):
    if not net or SUPPLY.match(net):
        return []
    return sorted({p for p, _ in nl.nets.get(net, {}).get("members", []) if ICS.match(p)})


def _where(nl, nets, pads):
    out = []
    for n in nets:
        mem = nl.nets.get(n, {}).get("members", [])
        if SUPPLY.match(n):
            out.append(f"{n} ({len(mem)} pads)")
            continue
        mem = sorted(mem, key=lambda m: (not ICS.match(m[0]), m[0], m[1]))
        txt = ", ".join(f"{p}.{q}" for p, q in mem[:10]) + (f", +{len(mem) - 10}" if len(mem) > 10 else "")
        out.append(f"{n}: {txt}" if txt else n)
    extra = [f"{p}.{q}" for p, q in pads if not any(p == m[0] and str(q) == str(m[1])
                                                     for n in nets for m in nl.nets.get(n, {}).get("members", []))]
    if extra:
        out.append(", ".join(extra))
    return "; ".join(out)


def _mk(nl, kind, title, detail="", nets=(), pads=(), parts=(), pairs=(), links=(), colours=None, source="",
        focus="pads"):
    nets = [n for n in dict.fromkeys(nets) if n]
    pads = [[str(p), str(q)] for p, q in pads if p and q is not None]
    allparts = list(parts) + [p for p, _ in pads] + [x[0] for x in pairs]
    for n in nets:
        allparts += _net_parts(nl, n)
    return {"kind": kind, "title": title, "detail": detail, "where": _where(nl, nets, pads),
            "nets": nets, "pads": pads, "parts": list(dict.fromkeys(p for p in allparts if p)),
            "pairs": [[str(a), str(b), str(c)] for a, b, c in pairs],
            "links": [[str(x) for x in ln] for ln in links],
            "colours": colours or {n: i for i, n in enumerate(nets)}, "source": source,
            "focus": focus}


def _cpld_pad(nl, name):
    pin = nl.cpld_pin_by_name(name) if name else None
    return [(CPLD, pin)] if pin else []


def _board_findings(nl, bd):
    out = []
    ch = bd.get("chain") or {}
    jt = [(CPLD, p) for n in JTAG_NETS for p in nl.nets.get(n, {}).get("cpld_pins", [])]
    jt += [("PI_HEADER", q) for n in JTAG_NETS for p, q in nl.nets.get(n, {}).get("members", []) if p == "PI_HEADER"]
    if ch:
        if ch.get("idcode_ok") is False or (ch.get("ok") is False and "idcode" not in ch):
            out.append(_mk(nl, "JTAG chain", ch.get("error") or f"unexpected IDCODE {ch.get('idcode')}",
                           "The CPLD did not answer on JTAG as an XC95288XL: check the Pi header JTAG wiring "
                           "(TCK/TMS/TDI/TDO), the CPLD's 3.3 V supply and that the board is powered.",
                           JTAG_NETS, jt, [CPLD, "PI_HEADER", "JTAG1"], source="chain"))
        if ch.get("bypass_ok") is False:
            out.append(_mk(nl, "JTAG chain", "BYPASS is not 1 bit",
                           "BYPASS did not read back as 1 bit: the TDI -> CPLD -> TDO path looks broken or noisy.",
                           ["TDI", "TDO"], [p for p in jt if nl.net_of(*p) in ("TDI", "TDO")],
                           [CPLD, "PI_HEADER"], source="chain"))
        bl = ch.get("boundary_length")
        if bl is not None and bl != (nl.d["cpld"].get("boundary_length") or 864):
            out.append(_mk(nl, "JTAG chain", f"boundary register {bl} bits (expected 864)",
                           "The boundary-scan register length is not the XC95288XL's: a different device, or "
                           "a broken TDI/TDO path.", ["TDI", "TDO"], [], [CPLD], source="chain"))
    for name, e in (bd.get("sample_normal") or {}).items():
        if not isinstance(e, dict):
            continue
        net = e.get("net") or resolve_net(nl, name)
        pads = [(CPLD, e["pin"])] if e.get("pin") is not None else _cpld_pad(nl, name)
        if e.get("finding"):
            out.append(_mk(nl, "stuck pin" if e.get("state") in ("high", "low") else "SAMPLE",
                           f"{name}: {e.get('finding')}" if isinstance(e["finding"], str) else f"{name}: {e.get('state')}",
                           e.get("msg") or f"SAMPLE snapshot (normal image): {e.get('state')}", [net], pads,
                           source="sample_normal"))
        elif name in SAMPLE_CLOCKS and e.get("state") in ("high", "low"):
            out.append(_mk(nl, "clock", f"{name} not toggling ({e['state']} in all {e.get('samples', '?')} SAMPLE snapshots)",
                           f"With the normal image running the {name} clock reads {e['state']} in every SAMPLE "
                           "snapshot; a running clock reads both levels. Look at its source and the track to "
                           f"CPLD pin {e.get('pin')}.", [net], pads, source="sample_normal"))
    if bd.get("cpu_in_reset") is False:
        out.append(_mk(nl, "reset", "CPU_RSTI did not read low with the Amiga held in reset",
                       "RESET -> IC10 -> CPU_RSTI should be low after the reset request; the 060 may be driving "
                       "its bus, so 060-side nets were not driven. Look at IC10 and CPU_RSTI.",
                       ["CPU_RSTI", "RESET"], _cpld_pad(nl, "CPU_RSTI") + [("IC10", "4")], ["IC10", CPU],
                       source="cpu_in_reset"))
    ex = bd.get("extest") or {}
    msgs = {r.get("name"): r for r in ex.get("nets", []) if isinstance(r, dict)}
    seen = set()
    for f in ex.get("findings", []) + bd.get("findings", []):
        if not isinstance(f, dict):
            continue
        k = f.get("kind")
        a = f.get("a") or f.get("net") or f.get("name")
        na = resolve_net(nl, a)
        rec = msgs.get(a) or {}
        if k == "stuck":
            sup = [tuple(x) for x in f.get("supply_neighbours") or []]
            pin = rec.get("cpld_pin") or nl.cpld_pin_by_name(a)
            pairs = [(CPLD, pin, n) for n, _ in sup] if pin else []
            hint = "".join(f" Neighbour pin {n} is {sn}: likely a solder bridge." for n, sn in sup)
            out.append(_mk(nl, "stuck pin", f"{a} held {f.get('level', '?')}",
                           f"CPLD pin {pin} does not follow when driven 0/1 in EXTEST: held {f.get('level')} "
                           f"(short to a supply or a driver, or a damaged CPLD output).{hint}",
                           [na], _cpld_pad(nl, a), pairs=pairs, source="extest"))
        elif k == "short":
            b = f.get("b")
            nb = resolve_net(nl, b)
            key = frozenset((na, nb))
            if key in seen:
                continue
            seen.add(key)
            adj = [tuple(x) for x in f.get("adjacent") or []] or (nl.adjacent(na, nb) if na and nb else [])
            hint = (" Adjacent pins " + ", ".join(f"{p} {x}/{y}" for p, x, y in adj) +
                    ": likely a solder bridge.") if adj else " No adjacent pads: look along both tracks and vias."
            out.append(_mk(nl, "short", f"{a} shorted to {b}",
                           f"{b} follows when {a} is driven 0/1 in EXTEST.{hint}", [na, nb],
                           _cpld_pad(nl, a) + _cpld_pad(nl, b), pairs=adj, source="extest"))
        elif k == "open":
            fp = str(f.get("fpga_pin"))
            cp = rec.get("cpld_pin") or nl.cpld_pin_by_name(a)
            out.append(_mk(nl, "open", f"{a}: open between CPLD pin {cp} and FPGA pin {fp}",
                           f"The CPLD drives {a} 0/1 in EXTEST but FPGA pin {fp} does not follow: an open "
                           "(lifted pin, cracked joint or broken track/via) between the two.",
                           [na], [(CPLD, cp), (FPGA, fp)], links=[(CPLD, cp, FPGA, fp)], source="extest"))
        else:
            nets = [na, resolve_net(nl, f.get("b"))]
            out.append(_mk(nl, str(k or "finding"), f"{a or '?'}: {k}",
                           f.get("msg") or json.dumps(f, default=str)[:300], nets, _cpld_pad(nl, a),
                           source="extest"))
    for r in ex.get("nets", []):
        if isinstance(r, dict) and r.get("status") == "finding" and not any(
                x["source"] == "extest" and resolve_net(nl, r.get("name")) in x["nets"] for x in out):
            na = r.get("net") or resolve_net(nl, r.get("name"))
            out.append(_mk(nl, "net", f"net {r.get('name')}", r.get("msg", ""), [na],
                           [(CPLD, r["cpld_pin"])] if r.get("cpld_pin") else [], source="extest"))
    fc = ex.get("float_changed") or []
    if fc:
        out.append(_mk(nl, "float", f"{len(fc)} pins read differently at the end of EXTEST",
                       "With every CPLD output off, these pins read differently at the end than at the start "
                       "(floating inputs or a slow leak): " + ", ".join(map(str, fc[:24])),
                       [resolve_net(nl, n) for n in fc[:24]], [p for n in fc[:24] for p in _cpld_pad(nl, n)],
                       source="extest"))
    return out


def data_lines(r):
    """Data lines (0-31, 060 numbering) whose bits differ in an exerciser result, or None."""
    flags = r.get("flags") or []
    if "exception" in flags or "summary" in flags:
        return None
    try:
        x = int(str(r["expected"]), 16) ^ int(str(r["got"]), 16)
        addr = int(str(r.get("addr", "0")), 16)
    except (KeyError, ValueError, TypeError):
        return None
    size = r.get("size") if r.get("size") in (1, 2, 4) else 4
    shift = 8 * (4 - size - (addr & 3))
    if shift < 0:
        shift = 0
    x &= (1 << 8 * size) - 1
    return [b + shift for b in range(8 * size) if x >> b & 1]


def _timing_findings(nl, tm):
    out = []
    for c in (tm.get("clocks") or {}).get("clocks", []):
        if not isinstance(c, dict) or c.get("status") != "finding":
            continue
        nets, pads = CLOCK_GEOMETRY.get(c.get("name"), ([c.get("name")] if c.get("name") in nl.nets else [], []))
        what = "missing" if c.get("mhz") in (0, 0.0, None) and c.get("name") != "pll_ratio" else "off"
        out.append(_mk(nl, "clock", f"clock {c.get('name')} {what}", c.get("msg", ""), nets, pads,
                       [FPGA, "OSC2"], source="clocks", focus="nets"))
    groups = {}
    for r in tm.get("results", []):
        if not isinstance(r, dict) or not r.get("failure", True):
            continue
        lines = data_lines(r)
        key = (r.get("group"), tuple(lines) if lines else (r.get("addr"), r.get("got")))
        groups.setdefault(key, []).append(r)
    for (group, _), rs in groups.items():
        r = rs[0]
        lines = data_lines(r)
        presets = sorted({x.get("preset") for x in rs}, key=str)
        pre = ", ".join(f"p{p}" for p in presets)
        n = f" ({len(rs)} results)" if len(rs) > 1 else ""
        if not lines:
            what = ("exception" if "exception" in (r.get("flags") or []) else
                    "checks read back wrong" if "summary" in (r.get("flags") or []) else "no differing bits")
            out.append(_mk(nl, "exerciser", f"{group}: {what} ({pre}){n}",
                           f"{r.get('size')}-byte access at {r.get('addr')}: expected {r.get('expected')}, got "
                           f"{r.get('got')}. No data lane to show.", source="exerciser"))
            continue
        nets, colours = [], {}
        for i, b in enumerate(lines):
            for nm in (f"DD{b}", f"D{b}"):
                if nm in nl.nets:
                    nets.append(nm)
                    colours[nm] = i
        names = ", ".join(f"D{b}" for b in lines)
        out.append(_mk(nl, "data lane", f"{group}: wrong data on {names} ({pre}){n}",
                       f"{r.get('size')}-byte access at {r.get('addr')}: expected {r.get('expected')}, got "
                       f"{r.get('got')}. The differing bits are 060 data lines {names}: shown are the 060 side "
                       "(DD: 68060, SDRAMs, CPLD) and the Amiga side (D: CPLD, slot, buffers). An 8- or 16-bit "
                       "Amiga port carries its byte lanes on other Amiga-side lines.",
                       nets, [p for nm in nets for p in _cpld_pad(nl, nm)], [CPU, CPLD], colours=colours,
                       source="exerciser", focus="nets"))
    for h in tm.get("hangs", []):
        if isinstance(h, dict):
            out.append(_mk(nl, "hang", f"preset {h.get('preset')}: hang in {h.get('group', h.get('phase'))}",
                           h.get("why", "the Amiga stopped at this clock preset"), source="exerciser"))
    return out


def findings(data, netlist=None):
    """Board/timing results -> findings with geometry. Never raises on odd input: what it
    cannot place is listed without a board location."""
    nl = netlist or NL.load()
    out = []
    for fn, key in ((_board_findings, "board"), (_timing_findings, "timing")):
        part = (data or {}).get(key)
        if not isinstance(part, dict):
            continue
        try:
            out += fn(nl, part)
        except Exception as e:                          # keep the report going
            out.append(_mk(nl, "note", f"{key} results not placed on the board", f"{type(e).__name__}: {e}",
                           source=key))
    for i, f in enumerate(out):
        f["id"] = f"f{i}"
    return out


# ---- SVG -------------------------------------------------------------------------------------

def _n(v):
    return f"{v:.2f}".rstrip("0").rstrip(".")


def _outline_path(segs):
    """Chain the outline segments into closed loops (path d)."""
    left = [((a, b), (c, d)) for a, b, c, d in segs]
    out = []
    while left:
        (p, q) = left.pop(0)
        loop = [p, q]
        while True:
            for i, (u, v) in enumerate(left):
                if math.dist(u, loop[-1]) < 0.15:
                    loop.append(v)
                elif math.dist(v, loop[-1]) < 0.15:
                    loop.append(u)
                else:
                    continue
                left.pop(i)
                break
            else:
                break
        out.append(loop)
    return out


def svg(board=None):
    b = board or load_board()
    x0, y0, x1, y1 = b["bbox"]
    Y = lambda y: y0 + y1 - y
    m = 2.0
    vb = (x0 - m, y0 - m, x1 - x0 + 2 * m, y1 - y0 + 2 * m)
    esc = lambda s: html.escape("" if s is None else str(s), quote=True)
    o = [f'<svg class="tfbv-svg" xmlns="http://www.w3.org/2000/svg" viewBox="{" ".join(_n(v) for v in vb)}" '
         f'data-flipw="{_n(x0 + x1)}" role="img" aria-label="TF4060 r5 board">',
         '<g class="tfbv-flip">']
    loops = _outline_path(b["outline"])
    d = " ".join("M" + " L".join(f"{_n(x)} {_n(Y(y))}" for x, y in lp) + " Z" for lp in loops)
    o.append(f'<path class="bd" d="{d}"/>')
    for x, y, dr in b.get("holes", []):
        o.append(f'<circle class="hole" cx="{_n(x)}" cy="{_n(Y(y))}" r="{_n(dr / 2)}"/>')
    parts = sorted(b["parts"].items(), key=lambda kv: (kv[1]["side"] == "top", kv[0] in LABELS))
    labels = []
    majors = [(n, p) for n, p in parts if n in LABELS]
    # a major part with another-side major on top of it (IC2 under IC8): its label shows from its own side only
    under = {n for n, p in majors for n2, p2 in majors if p2["side"] != p["side"]
             and math.dist(p["body"][:2], p2["body"][:2]) < 4}
    o.append('<g class="parts">')
    for name, p in parts:
        cx, cy, w, h, a = p["body"]
        side = "top" if p["side"] == "top" else "bot"
        cls = f"pt {side}" + (" major" if name in LABELS else "")
        rx = min(w, h) * (0.08 if name in LABELS else 0.2)
        o.append(f'<rect class="{cls}" data-part="{esc(name)}" data-v="{esc(p.get("value"))}" '
                 f'data-pk="{esc(p.get("package"))}" x="{_n(cx - w / 2)}" y="{_n(Y(cy) - h / 2)}" '
                 f'width="{_n(w)}" height="{_n(h)}" rx="{_n(rx)}"'
                 + (f' transform="rotate({_n(-a)} {_n(cx)} {_n(Y(cy))})"' if a % 360 else "") + "/>")
        la = a + 90 if h > w * 1.15 else a
        la = (la + 90) % 180 - 90                       # readable: -90..90
        lng, sht = max(w, h), min(w, h)
        if name in LABELS:
            func = LABELS[name]
            fs = min(sht * 0.26, lng * 0.95 / max(len(name), 0.62 * len(func)) / 0.6, 3.2)
            body = (f'<text class="ln" y="{_n(-fs * 0.05)}" font-size="{_n(fs)}">{esc(name)}</text>'
                    f'<text class="lf" y="{_n(fs * 0.85)}" font-size="{_n(fs * 0.62)}">{esc(func)}</text>')
            cls = "lb major " + side + (" ovl" if name in under else "")
        else:
            fs = min(sht * 0.55, lng * 0.9 / max(1, len(name)) / 0.6, 0.9)
            body = f'<text class="ls" y="{_n(fs * 0.35)}" font-size="{_n(fs)}">{esc(name)}</text>'
            cls = "lb small " + side
        labels.append(f'<g class="{cls}" data-x="{_n(cx)}" data-y="{_n(Y(cy))}" data-a="{_n(la)}" '
                      f'transform="translate({_n(cx)} {_n(Y(cy))}) rotate({_n(-la)})">{body}</g>')
    o.append("</g><g class=\"pads\">")
    for name, p in parts:
        o.append(f'<g data-part="{esc(name)}">')
        for pad, x, y, size, layer, net in p["pads"]:
            r = max(0.13, min(0.75, size / 2))
            o.append(f'<circle class="{layer}" cx="{_n(x)}" cy="{_n(Y(y))}" r="{_n(r)}" data-p="{esc(pad)}"'
                     + (f' data-n="{esc(net)}"' if net else "") + "/>")
        o.append("</g>")
    o.append('</g><g class="rats"></g><g class="marks"></g></g>')
    o.append('<g class="labels">' + "".join(labels) + "</g></svg>")
    return "\n".join(o)


CSS = """
.tfbv{--bv-bg:#e9eeea;--bv-board:#145a32;--bv-edge:#3c8d5c;--bv-body:#1f2622;--bv-bodyline:#8fa597;
--bv-small:#2f4a3a;--bv-pad:#d4b04c;--bv-text:#eef3ef;--bv-mut:#9fb8a8;--bv-hole:#0b0f0c;
--bv-c0:#ff4b4b;--bv-c1:#2fc4ff;--bv-c2:#ffb000;--bv-c3:#c77dff;--bv-c4:#57e36b;--bv-c5:#ff7ac8;
--bv-pair:#fff200;--bv-part:#ffffff;--bv-tipbg:#1d1d1f;--bv-tipfg:#f5f5f5;--bv-sel:rgba(255,176,0,.18);
position:relative;margin:8px 0 24px}
@media (prefers-color-scheme:dark){.tfbv{--bv-bg:#0c0f0d;--bv-board:#0f4426;--bv-edge:#2a6b45;--bv-body:#141916;
--bv-small:#24392c;--bv-tipbg:#f2f2f2;--bv-tipfg:#111;--bv-sel:rgba(255,176,0,.16)}}
.tfbv .tfbv-bar{display:flex;flex-wrap:wrap;gap:6px;align-items:center;margin:0 0 6px}
.tfbv button{font:inherit;font-size:12px;padding:3px 9px;border:1px solid var(--line,#bbb);border-radius:6px;
background:transparent;color:inherit;cursor:pointer}
.tfbv button[aria-pressed=true]{background:var(--bv-sel);border-color:var(--bv-c2)}
.tfbv .tfbv-sel{font-size:12px;color:var(--mut,#666);flex:1 1 200px;min-width:0}
.tfbv .tfbv-key{display:inline-block;width:10px;height:10px;border-radius:50%;vertical-align:-1px;margin:0 3px 0 8px}
.tfbv-svg{display:block;width:100%;height:auto;background:var(--bv-bg);border-radius:8px;touch-action:none;
cursor:grab;user-select:none;-webkit-user-select:none}
.tfbv-svg.drag{cursor:grabbing}
.tfbv-svg .bd{fill:var(--bv-board);stroke:var(--bv-edge);stroke-width:.4}
.tfbv-svg .hole{fill:var(--bv-hole);stroke:var(--bv-edge);stroke-width:.2}
.tfbv-svg .pt{fill:var(--bv-small);stroke:var(--bv-bodyline);stroke-width:.08;opacity:.75}
.tfbv-svg .pt.major{fill:var(--bv-body);stroke-width:.18;opacity:1}
.tfbv-svg .pt.bot{fill:none;stroke-dasharray:.6 .35;opacity:.45}
.tfbv.flip .tfbv-svg .pt.bot{fill:var(--bv-small);stroke-dasharray:none;opacity:.75}
.tfbv.flip .tfbv-svg .pt.bot.major{fill:var(--bv-body);opacity:1}
.tfbv.flip .tfbv-svg .pt.top{fill:none;stroke-dasharray:.6 .35;opacity:.4}
.tfbv-svg circle[data-p]{fill:var(--bv-pad);opacity:.9}
.tfbv-svg circle.b{opacity:.28}
.tfbv.flip .tfbv-svg circle.b{opacity:.9}
.tfbv.flip .tfbv-svg circle.t{opacity:.28}
.tfbv-svg .labels{pointer-events:none;text-anchor:middle;font-family:system-ui,sans-serif}
.tfbv-svg .ln{fill:var(--bv-text);font-weight:600}
.tfbv-svg .lf{fill:var(--bv-mut)}
.tfbv-svg .ls{fill:var(--bv-mut);display:none}
.tfbv.zoom .tfbv-svg .ls{display:inline}
.tfbv-svg .lb.bot .ln,.tfbv-svg .lb.bot .lf{opacity:.55}
.tfbv.flip .tfbv-svg .lb.bot .ln,.tfbv.flip .tfbv-svg .lb.bot .lf{opacity:1}
.tfbv.flip .tfbv-svg .lb.top .ln,.tfbv.flip .tfbv-svg .lb.top .lf{opacity:.5}
.tfbv-svg .lb.ovl.bot,.tfbv.flip .tfbv-svg .lb.ovl.top{display:none}
.tfbv.flip .tfbv-svg .lb.ovl.bot{display:inline}
.tfbv.hl .tfbv-svg circle[data-p]{opacity:.18}
.tfbv.hl .tfbv-svg .pt{opacity:.35}
.tfbv.hl .tfbv-svg .pt.on{opacity:1;stroke:var(--bv-part);stroke-width:2px;vector-effect:non-scaling-stroke;stroke-dasharray:none}
.tfbv-svg circle[data-p].on{opacity:1 !important;stroke:#000;stroke-width:.06}
.tfbv-svg .c0{fill:var(--bv-c0) !important;stroke:var(--bv-c0)}.tfbv-svg .c1{fill:var(--bv-c1) !important;stroke:var(--bv-c1)}
.tfbv-svg .c2{fill:var(--bv-c2) !important;stroke:var(--bv-c2)}.tfbv-svg .c3{fill:var(--bv-c3) !important;stroke:var(--bv-c3)}
.tfbv-svg .c4{fill:var(--bv-c4) !important;stroke:var(--bv-c4)}.tfbv-svg .c5{fill:var(--bv-c5) !important;stroke:var(--bv-c5)}
.tfbv-svg .rats line,.tfbv-svg .marks *{vector-effect:non-scaling-stroke;fill:none}
.tfbv-svg .rats line{stroke-width:1.5px;opacity:.85}
.tfbv-svg .marks .ring{stroke:#fff;stroke-width:2px}
.tfbv-svg .marks .ring.pair{stroke:var(--bv-pair);stroke-width:2.5px}
.tfbv-svg .marks line.pair{stroke:var(--bv-pair);stroke-width:2.5px}
.tfbv-svg .marks line.open{stroke:#fff;stroke-width:2px;stroke-dasharray:5 4}
.tfbv-svg .rats line.c0,.tfbv-svg .rats line.c1,.tfbv-svg .rats line.c2,.tfbv-svg .rats line.c3,
.tfbv-svg .rats line.c4,.tfbv-svg .rats line.c5{fill:none !important}
.tfbv .tfbv-tip{position:absolute;pointer-events:none;background:var(--bv-tipbg);color:var(--bv-tipfg);
font-size:12px;padding:3px 7px;border-radius:5px;white-space:nowrap;display:none;z-index:5}
.tfbv table{border-collapse:collapse;width:100%;margin:10px 0 0}
.tfbv td,.tfbv th{border-bottom:1px solid var(--line,#ddd);padding:4px 6px;text-align:left;vertical-align:top}
.tfbv tbody tr{cursor:pointer}
.tfbv tbody tr:hover{background:var(--bv-sel)}
.tfbv tbody tr.sel{background:var(--bv-sel);box-shadow:inset 3px 0 0 var(--bv-c2)}
.tfbv tbody tr.nogeo{cursor:default}
.tfbv .k{font-weight:600;font-size:12px;white-space:nowrap}
.tfbv .w{font-size:12px;word-break:break-word}
.tfbv .d,.tfbv .m{color:var(--mut,#666);font-size:12px}
.tfbv .wrap{overflow-x:auto}
"""

JS = r"""
(function(){
var root=document.getElementById(UID);if(!root)return;
var svg=root.querySelector('svg'),flipG=svg.querySelector('.tfbv-flip'),rats=svg.querySelector('.rats'),
marks=svg.querySelector('.marks'),tip=root.querySelector('.tfbv-tip'),selTxt=root.querySelector('.tfbv-sel');
var F=JSON.parse(root.querySelector('script.tfbv-data').textContent);
var NS='http://www.w3.org/2000/svg',W=parseFloat(svg.getAttribute('data-flipw'));
var VB0=svg.getAttribute('viewBox').split(' ').map(parseFloat),view=VB0.slice(),flipped=false,cur=null;
var byNet={},byPad={},byPart={};
Array.prototype.forEach.call(svg.querySelectorAll('circle[data-p]'),function(c){
 var pt=c.parentNode.getAttribute('data-part'),n=c.getAttribute('data-n');byPad[pt+'.'+c.getAttribute('data-p')]=c;
 if(n)(byNet[n]=byNet[n]||[]).push(c);});
Array.prototype.forEach.call(svg.querySelectorAll('rect[data-part]'),function(r){byPart[r.getAttribute('data-part')]=r;});
function el(t,a,p){var e=document.createElementNS(NS,t);for(var k in a)e.setAttribute(k,a[k]);p.appendChild(e);return e;}
function pos(c){return{x:parseFloat(c.getAttribute('cx')),y:parseFloat(c.getAttribute('cy')),r:parseFloat(c.getAttribute('r'))};}
function setView(){svg.setAttribute('viewBox',view.join(' '));root.classList.toggle('zoom',view[2]<VB0[2]/3);}
function labels(){Array.prototype.forEach.call(svg.querySelectorAll('.labels .lb'),function(g){
 var x=parseFloat(g.getAttribute('data-x')),y=g.getAttribute('data-y'),a=parseFloat(g.getAttribute('data-a'));
 g.setAttribute('transform','translate('+(flipped?W-x:x)+' '+y+') rotate('+(flipped?a:-a)+')');});}
function flip(on){if(on===flipped)return;flipped=on;root.classList.toggle('flip',on);
 if(on)flipG.setAttribute('transform','matrix(-1 0 0 1 '+W+' 0)');else flipG.removeAttribute('transform');
 view[0]=W-view[0]-view[2];setView();labels();
 root.querySelector('[data-act=top]').setAttribute('aria-pressed',String(!on));
 root.querySelector('[data-act=bottom]').setAttribute('aria-pressed',String(on));}
function clear(){root.classList.remove('hl');while(rats.firstChild)rats.removeChild(rats.firstChild);
 while(marks.firstChild)marks.removeChild(marks.firstChild);
 Array.prototype.forEach.call(svg.querySelectorAll('.on'),function(e){e.setAttribute('class',(e.getAttribute('class')||'').replace(/\s*\b(on|c\d)\b/g,''));});
 Array.prototype.forEach.call(root.querySelectorAll('tr.sel'),function(r){r.classList.remove('sel');});
 cur=null;selTxt.textContent=selTxt.getAttribute('data-idle');}
function addc(e,c){e.setAttribute('class',((e.getAttribute('class')||'')+' '+c).trim());}
function mst(cs,col){var P=cs.map(pos),n=P.length;if(n<2||n>RATS)return;var inT=[true],d=[],from=[],i,j;
 for(i=1;i<n;i++){inT[i]=false;d[i]=Math.hypot(P[i].x-P[0].x,P[i].y-P[0].y);from[i]=0;}
 for(var k=1;k<n;k++){var b=-1;for(i=1;i<n;i++)if(!inT[i]&&(b<0||d[i]<d[b]))b=i;inT[b]=true;
  el('line',{x1:P[from[b]].x,y1:P[from[b]].y,x2:P[b].x,y2:P[b].y,'class':'c'+col},rats);
  for(j=1;j<n;j++)if(!inT[j]){var dd=Math.hypot(P[j].x-P[b].x,P[j].y-P[b].y);if(dd<d[j]){d[j]=dd;from[j]=b;}}}}
function ring(c,cls){var p=pos(c);el('circle',{cx:p.x,cy:p.y,r:p.r+0.45,'class':'ring'+(cls?' '+cls:'')},marks);return p;}
function fit(pts){if(!pts.length)return;var a=1e9,b=1e9,c=-1e9,d=-1e9;pts.forEach(function(p){
 var x=flipped?W-p.x:p.x;a=Math.min(a,x);b=Math.min(b,p.y);c=Math.max(c,x);d=Math.max(d,p.y);});
 var w=Math.max(c-a+10,40),h=Math.max(d-b+10,40*VB0[3]/VB0[2]),asp=VB0[2]/VB0[3];
 if(w/h<asp)w=h*asp;else h=w/asp;if(w>VB0[2]){w=VB0[2];h=VB0[3];}
 view=[(a+c)/2-w/2,(b+d)/2-h/2,w,h];setView();}
function show(f,tr){clear();cur=f;root.classList.add('hl');if(tr)tr.classList.add('sel');var pts=[],key=[];
 (f.nets||[]).forEach(function(n,i){var col=(f.colours&&f.colours[n]!=null?f.colours[n]:i)%6,cs=byNet[n]||[];
  cs.forEach(function(c){addc(c,'on c'+col);pts.push(pos(c));});mst(cs,col);});
 (f.parts||[]).forEach(function(p){if(byPart[p])addc(byPart[p],'on');});
 (f.pads||[]).forEach(function(q){var c=byPad[q[0]+'.'+q[1]];if(c){addc(c,'on');key.push(ring(c));}});
 (f.pairs||[]).forEach(function(q){var a=byPad[q[0]+'.'+q[1]],b=byPad[q[0]+'.'+q[2]];if(a&&b){var pa=ring(a,'pair'),pb=ring(b,'pair');
  el('line',{x1:pa.x,y1:pa.y,x2:pb.x,y2:pb.y,'class':'pair'},marks);key.push(pa,pb);}});
 (f.links||[]).forEach(function(q){var a=byPad[q[0]+'.'+q[1]],b=byPad[q[2]+'.'+q[3]];if(a&&b){var pa=pos(a),pb=pos(b);
  el('line',{x1:pa.x,y1:pa.y,x2:pb.x,y2:pb.y,'class':'open'},marks);}});
 if(!pts.length&&!key.length)(f.parts||[]).forEach(function(p){var r=byPart[p];if(r){var bb=r.getBBox();pts.push({x:bb.x,y:bb.y},{x:bb.x+bb.width,y:bb.y+bb.height});}});
 fit(key.length&&f.focus!=='nets'?key:pts.concat(key));
 selTxt.textContent=f.title+(f.where?'  —  '+f.where:'');}
function svgPt(e){var r=svg.getBoundingClientRect(),s=Math.max(view[2]/r.width,view[3]/r.height);
 var ox=(r.width-view[2]/s)/2,oy=(r.height-view[3]/s)/2;
 return{x:view[0]+(e.clientX-r.left-ox)*s,y:view[1]+(e.clientY-r.top-oy)*s,s:s};}
function zoom(k,p){var w=Math.min(Math.max(view[2]*k,4),VB0[2]*1.5),f=w/view[2];
 p=p||{x:view[0]+view[2]/2,y:view[1]+view[3]/2};view=[p.x-(p.x-view[0])*f,p.y-(p.y-view[1])*f,w,view[3]*f];setView();}
svg.addEventListener('wheel',function(e){e.preventDefault();zoom(e.deltaY>0?1.2:1/1.2,svgPt(e));},{passive:false});
var drag=null;
svg.addEventListener('pointerdown',function(e){drag={x:e.clientX,y:e.clientY,v:view.slice(),s:svgPt(e).s,moved:false};
 try{svg.setPointerCapture(e.pointerId);}catch(x){}});
svg.addEventListener('pointermove',function(e){if(drag){var dx=e.clientX-drag.x,dy=e.clientY-drag.y;
 if(Math.abs(dx)+Math.abs(dy)>3){drag.moved=true;svg.classList.add('drag');}
 if(drag.moved){view=[drag.v[0]-dx*drag.s,drag.v[1]-dy*drag.s,drag.v[2],drag.v[3]];setView();}}else hover(e);});
svg.addEventListener('pointerup',function(e){var d=drag;drag=null;svg.classList.remove('drag');
 if(d&&!d.moved){var t=document.elementFromPoint(e.clientX,e.clientY);
  if(t&&t.getAttribute&&t.getAttribute('data-p')){var n=t.getAttribute('data-n'),pt=t.parentNode.getAttribute('data-part');
   if(n)show({title:'net '+n,where:pt+'.'+t.getAttribute('data-p'),nets:[n],pads:[[pt,t.getAttribute('data-p')]],parts:[pt]});}}});
svg.addEventListener('pointerleave',function(){tip.style.display='none';});
function hover(e){var t=e.target,s='';if(t.getAttribute('data-p')){var pt=t.parentNode.getAttribute('data-part'),
 l=t.getAttribute('class')||'';s=pt+' pin '+t.getAttribute('data-p')+' · '+(t.getAttribute('data-n')||'no net')+
 ' · '+(/\bth\b/.test(l)?'through-hole':/\bb\b/.test(l)?'bottom':'top');}
 else if(t.getAttribute('data-part')){s=t.getAttribute('data-part')+' · '+(t.getAttribute('data-v')||'')+
 ' · '+(t.getAttribute('data-pk')||'')+(/\bbot\b/.test(t.getAttribute('class'))?' · bottom':'');}
 if(!s){tip.style.display='none';return;}var r=root.getBoundingClientRect();tip.textContent=s;tip.style.display='block';
 var x=e.clientX-r.left+12;if(x+tip.offsetWidth>r.width)x=Math.max(0,e.clientX-r.left-tip.offsetWidth-12);
 tip.style.left=x+'px';tip.style.top=(e.clientY-r.top+14)+'px';}
root.addEventListener('click',function(e){var b=e.target.closest?e.target.closest('[data-act],tr[data-f]'):null;if(!b)return;
 var a=b.getAttribute('data-act');
 if(a==='top')flip(false);else if(a==='bottom')flip(true);else if(a==='fit'){view=VB0.slice();if(flipped)view[0]=W-view[0]-view[2];setView();}
 else if(a==='clear')clear();else if(a==='in')zoom(1/1.5);else if(a==='out')zoom(1.5);
 else if(b.hasAttribute('data-f')){var f=F[parseInt(b.getAttribute('data-f'),10)];
  if(cur===f){clear();return;}if(!f.nets.length&&!f.pads.length&&!f.parts.length){clear();b.classList.add('sel');
  selTxt.textContent=f.title+' — no board location';return;}
  show(f,b);var r=svg.getBoundingClientRect();if(r.bottom<0||r.top>(window.innerHeight||0))svg.scrollIntoView({block:'nearest'});}});
root.addEventListener('keydown',function(e){if((e.key==='Enter'||e.key===' ')&&e.target.hasAttribute&&e.target.hasAttribute('data-f')){e.preventDefault();e.target.click();}});
clear();
})();
"""


def _js(uid):
    return JS.replace("UID", json.dumps(uid)).replace("RATS", str(RATS_MAX))


def _uid(payload):
    return "tfbv-" + hashlib.sha1(f"{next(_COUNTER)}:{payload}".encode()).hexdigest()[:8]


def html_section(data, board=None, netlist=None, heading="Board view"):
    """The board view block for report.html, or "" when there are no board results and no
    findings to place. Never raises: a broken board view must not cost the report."""
    try:
        data = data or {}
        fs = findings(data, netlist)
        if not data.get("board") and not fs:
            return ""
        return _section(fs, board, heading)
    except Exception as e:                                # pragma: no cover - defensive
        return f"<!-- board view not drawn: {html.escape(type(e).__name__)}: {html.escape(str(e))} -->"


def _section(fs, board=None, heading="Board view"):
    esc = lambda x: html.escape("" if x is None else str(x))
    payload = json.dumps(fs, separators=(",", ":"), default=str).replace("</", "<\\/")
    uid = _uid(payload)
    placed = sum(1 for f in fs if f["nets"] or f["pads"] or f["parts"])
    rows = []
    for i, f in enumerate(fs):
        geo = f["nets"] or f["pads"] or f["parts"]
        rows.append(f"<tr data-f='{i}' tabindex='0'{'' if geo else ' class=nogeo'}><td class='k'>{esc(f['kind'])}</td>"
                    f"<td>{esc(f['title'])}</td><td class='w'>{esc(f['where']) or '<span class=m>no board location</span>'}"
                    f"</td><td class='d'>{esc(f['detail'])}</td></tr>")
    if not rows:
        rows.append("<tr class='nogeo'><td colspan='4' class='m'>No board-test or exerciser findings to place "
                    "on the board.</td></tr>")
    idle = ("Click a finding to show it on the board; click a pad for its net; wheel or drag to zoom and pan."
            if fs else "No findings. Click a pad to see its net; wheel or drag to zoom and pan.")
    keys = "".join(f"<span class='tfbv-key' style='background:var(--bv-c{i})'></span>{t}" for i, t in
                   ((0, "net"), (1, "second net"))) + \
        "<span class='tfbv-key' style='background:var(--bv-pair)'></span>suspect neighbour pads"
    return "\n".join([
        f"<section class='tfbv' id='{uid}'>",
        f"<style>{CSS}</style>",
        f"<h2>{esc(heading)}</h2>",
        f"<p class='m'>TF4060 r5 board, seen from the top (bottom-side parts dashed). {placed} of {len(fs)} "
        f"findings placed on the board. Observations only, as above.{keys}</p>",
        "<div class='tfbv-bar'><button type='button' data-act='top' aria-pressed='true'>Top</button>"
        "<button type='button' data-act='bottom' aria-pressed='false'>Bottom</button>"
        "<button type='button' data-act='in' aria-label='zoom in'>+</button>"
        "<button type='button' data-act='out' aria-label='zoom out'>&minus;</button>"
        "<button type='button' data-act='fit'>Fit</button><button type='button' data-act='clear'>Clear</button>"
        f"<span class='tfbv-sel' data-idle='{esc(idle)}'></span></div>",
        svg(board),
        "<div class='tfbv-tip'></div>",
        "<div class='wrap'><table><thead><tr><th>Kind</th><th>What</th><th>Where (net: part.pin)</th>"
        "<th>Detail</th></tr></thead><tbody>",
        *rows,
        "</tbody></table></div>",
        f"<script type='application/json' class='tfbv-data'>{payload}</script>",
        f"<script>{_js(uid)}</script>",
        "</section>",
    ])


def page(data, title="TF4060 board view", board=None, netlist=None):
    """A standalone HTML page with the board view (same look as report.html)."""
    fs = findings(data, netlist)
    return ("<!doctype html><html><head><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width,initial-scale=1'>"
            f"<title>{html.escape(title)}</title><style>"
            ":root{--bg:#fff;--fg:#1d1d1f;--mut:#666;--line:#ddd}"
            "@media (prefers-color-scheme:dark){:root{--bg:#111;--fg:#eee;--mut:#aaa;--line:#333}}"
            "body{background:var(--bg);color:var(--fg);font:14px/1.4 system-ui,sans-serif;margin:0 auto;"
            "max-width:1100px;padding:16px}.m{color:var(--mut);font-size:12px}"
            f"</style></head><body><h1>{html.escape(title)}</h1>"
            + _section(fs, board) + "</body></html>")


# ---- demo ------------------------------------------------------------------------------------

def demo_results(netlist=None):
    """Fabricated board + timing results (for the demo page and the tests): a solder bridge
    between two adjacent CPLD data pins, an open on a CPLD-FPGA net, a stuck pin next to GND,
    a missing clock, wrong data on one lane, a hang."""
    nl = netlist or NL.load()
    sample = {"BCLK": {"pin": 30, "net": "BCLK", "state": "toggling", "high": 7, "samples": 16, "cpld_drives": False},
              "PCLK": {"pin": 38, "net": "PCLK_CPLD", "state": "toggling", "high": 9, "samples": 16,
                       "cpld_drives": False}}
    adj = nl.adjacent("DD4", "DD5")
    board = {
        "chain": {"idcode": "0x59616093", "idcode_ok": True, "usercode": "0x54460001", "bypass_ok": True,
                  "boundary_length": 864, "ok": True},
        "sample_normal": sample, "cpu_rsti_after_reset": [0, 0, 0, 0], "cpu_in_reset": True,
        "extest": {
            "nets": [
                {"name": "DD<4>", "net": "DD4", "cpld_pin": 6, "status": "finding",
                 "msg": "shorted to DD<5> (CPLD pin 7); adjacent pins " +
                        ", ".join(f"{p} {a}/{b}" for p, a, b in adj) + ": likely a solder bridge"},
                {"name": "TS", "net": "TS", "cpld_pin": 54, "status": "finding",
                 "msg": "FPGA pin 44 reads 0/0: open between CPLD pin 54 and FPGA pin 44"},
                {"name": "DD<15>", "net": "DD15", "cpld_pin": 19, "status": "finding",
                 "msg": "CPLD pin 19 reads 0/0 when driven 0/1: held low; neighbour pin 18 is GND: "
                        "likely a solder bridge"},
                {"name": "DD<0>", "net": "DD0", "cpld_pin": 2, "status": "info",
                 "msg": "drives 0 and 1, no other pin follows"}],
            "findings": [
                {"kind": "short", "a": "DD<4>", "b": "DD<5>", "adjacent": [list(x) for x in adj]},
                {"kind": "short", "a": "DD<5>", "b": "DD<4>", "adjacent": [list(x) for x in nl.adjacent("DD5", "DD4")]},
                {"kind": "open", "a": "TS", "fpga_pin": "44"},
                {"kind": "stuck", "a": "DD<15>", "level": "low", "supply_neighbours": [["18", "GND"]]}],
            "float": {}, "float_changed": []},
        "skipped": {},
    }
    timing = {
        "clocks": {"clocks": [
            {"name": "fabric", "mhz": 100.02, "nominal": 100.0, "status": "info", "msg": "FPGA PLL clock 100.02 MHz"},
            {"name": "CLKAUX", "nominal": 25.0, "count": 0, "gate": 4096, "mhz": 0.0, "status": "finding",
             "msg": "CLKAUX (25 MHz oscillator OSC2, second FPGA input): no edges counted: clock missing"}]},
        "results": [{"preset": 2, "test": 0x10, "group": "chip RAM", "size": 4, "flags": [], "failure": True,
                     "addr": "0x00012340", "expected": "0x12345678", "got": "0x12345e78"},
                    {"preset": 5, "test": 0x10, "group": "chip RAM", "size": 4, "flags": [], "failure": True,
                     "addr": "0x00012344", "expected": "0x00000000", "got": "0x00000800"}],
        "hangs": [{"preset": 5, "group": "CIA registers", "phase": "0x0050", "why": "heartbeat stopped"}],
        "ok": True,
    }
    return {"board": board, "timing": timing}


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python3 -m tfdiag.boardview", description="TF4060 r5 board view page")
    ap.add_argument("--dir", help="result directory (board/board.json, timing/timing.json)")
    ap.add_argument("--demo", action="store_true", help="fabricated findings instead of --dir")
    ap.add_argument("--out", required=True, help="HTML file to write")
    a = ap.parse_args(argv)
    if a.demo:
        data, title = demo_results(), "TF4060 board view (demo: fabricated findings)"
    elif a.dir:
        from . import report
        data, title = report.collect(a.dir), f"TF4060 board view: {a.dir}"
    else:
        ap.error("--dir or --demo")
    Path(a.out).write_text(page(data, title))
    print(a.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
