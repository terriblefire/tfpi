"""Render a tfdiag waveform VCD as an SVG timing diagram (stdlib only, runs on the Pi).

    python3 -m tfdiag vcd-svg timing/waves/wave_p0_k3_r1.vcd [--out x.svg] [--start-ns N]
                              [--span-ns N] [--all] [--all-signals]
    python3 -m tfdiag.vcdsvg FILE.vcd ...                  (the same, as a script)

Signals are rows, the time axis is in ns, the trigger is a red marker, and every
complete bus cycle (AS20 fall .. rise) in view is a band labelled with its DS20 -> DSACK
time (or AS20 -> STERM). With the address log in the VCD (ADDR/SIZ/RW/REGION vectors) the
label is e.g. "W.L $00012340 chip, DS->DSACK 360 ns" and an ADDR bus row is drawn;
without it, R/W and, for the trigger cycle, the capture's region. Default view: from 500 ns before the
trigger, 3 us wide. SVG only: convert with any SVG viewer if a PNG is needed.
"""

import argparse
import html
import math
import re
import sys
from pathlib import Path

from . import amiga
from . import protocol as P

KEY_SIGNALS = ["AS20", "DS20", "RW20", "DSACK0", "DSACK1", "TA_BUS", "TS"]
CSS = """
:root{--bg:#fff;--fg:#1d1d1f;--mut:#6b7280;--grid:#e5e7eb;--sig:#2563eb;--trig:#dc2626;--cyc:#2563eb14;--cyc2:#05966914;--edge:#9ca3af}
@media (prefers-color-scheme:dark){:root{--bg:#111827;--fg:#f3f4f6;--mut:#9ca3af;--grid:#374151;--sig:#60a5fa;--trig:#f87171;--cyc:#60a5fa22;--cyc2:#34d39922;--edge:#6b7280}}
svg{background:var(--bg);font:12px system-ui,sans-serif}
.lbl{fill:var(--fg)}.mut{fill:var(--mut)}.grid{stroke:var(--grid)}.sig{stroke:var(--sig);fill:none;stroke-width:1.5}
.trig{stroke:var(--trig);stroke-dasharray:4 3}.tt{fill:var(--trig)}.cyc{fill:var(--cyc)}.cyc2{fill:var(--cyc2)}
.edge{stroke:var(--edge);stroke-dasharray:2 3}.bus{fill:none;stroke:var(--sig);stroke-width:1.2}
"""


def parse_vcd(text):
    """{'signals': [names], 'changes': [(t_ps, {name: 0/1})], 'comment': str, 'end_ps': int}"""
    ids, order, comment = {}, [], ""
    m = re.search(r"\$comment\s+(.*?)\s*\$end", text, re.S)
    if m:
        comment = m.group(1)
    widths = {}
    for width, vid, name in re.findall(r"\$var\s+wire\s+(\d+)\s+(\S+)\s+(\S+)(?:\s+\[[^\]]*\])?\s+\$end", text):
        ids[vid] = name
        widths[name] = int(width)
        order.append(name)
    body = text.split("$enddefinitions $end", 1)[-1]
    changes, t, cur = [], 0, {}
    toks = body.split()
    i = 0
    while i < len(toks):
        tok = toks[i]
        if tok.startswith("#"):
            if cur:
                changes.append((t, cur))
                cur = {}
            t = int(tok[1:])
        elif tok[0] in "bB" and i + 1 < len(toks) and toks[i + 1] in ids:
            bits = tok[1:]
            cur[ids[toks[i + 1]]] = None if any(c in "xXzZ" for c in bits) else int(bits, 2)
            i += 1
        elif tok[0] in "01xXzZ" and tok[1:] in ids:
            cur[ids[tok[1:]]] = None if tok[0] in "xXzZ" else int(tok[0])
        i += 1
    if cur:
        changes.append((t, cur))
    return {"signals": order, "changes": changes, "comment": comment, "end_ps": t, "widths": widths}


def runs(vcd):
    """Run-length entries (state bits as protocol.WAVE_SIGNALS, duration in ps), plus the
    trigger time (ps) and per-signal level timelines."""
    state = {s: 1 for s in vcd["signals"]}
    lines = {s: [] for s in vcd["signals"]}
    times = []
    trig = None
    for t, ch in vcd["changes"]:
        state.update(ch)
        for s, v in ch.items():
            lines[s].append((t, v))
        if ch.get("TRIGGER") == 1 and trig is None:
            trig = t
        times.append((t, dict(state)))
    entries = []
    for (t0, st), (t1, _) in zip(times, times[1:] + [(vcd["end_ps"], None)]):
        word = 0
        for i, name in enumerate(P.WAVE_SIGNALS):
            v = st.get(name, 1)
            word |= (1 if v is None else v) << i
        if t1 > t0:
            entries.append((word, t1 - t0))
    return entries, trig, lines


def region_of(vcd, path=None):
    m = re.search(r"region (\w+)", vcd["comment"])
    if m and m.group(1) != "any":
        return m.group(1)
    if path:
        m = re.search(r"_k(\d)_r(\d)", Path(path).name)
        if m:
            kind, code = int(m.group(1)), int(m.group(2))
            if kind == 3:
                return amiga.REGION_BY_CODE.get(code)
            if kind == 4:
                return "ramsey"
    return None


def value_at(line, t_ps):
    """The value of a track at time t_ps (changes up to and including t_ps)."""
    v = None
    for t, x in line:
        if t > t_ps:
            break
        v = x
    return v


def log_label(lines, t_ps):
    """'W.L $00012340 chip' from the log vectors at a cycle start, or None."""
    if "ADDR" not in lines:
        return None
    a = value_at(lines["ADDR"], t_ps)
    if a is None:
        return None
    rw, siz, reg = (value_at(lines.get(n, []), t_ps) for n in ("RW", "SIZ", "REGION"))
    rname = amiga.REGION_BY_CODE.get(reg, "") if reg is not None else ""
    return f"{'R' if rw else 'W'}.{P.SIZ_NAMES.get(siz, '?')} ${a:08X}" + (f" {rname}" if rname else "")


CHAR_PX = 7.0      # rough width of one 12 px character


def fit_label(label, width):
    """The longest of: the label, without its timing, its first two words, its first word,
    that fits in width px (None if nothing does); the full label stays in the tooltip."""
    words = label.split(", ")[0].split(" ")
    for cand in (label, label.split(", ")[0], " ".join(words[:2]), words[0]):
        if len(cand) * CHAR_PX + 6 <= width:
            return cand
    return None


def nice_step(span_ns, target=10):
    raw = span_ns / target
    mag = 10 ** math.floor(math.log10(raw)) if raw > 0 else 1
    for f in (1, 2, 5, 10):
        if raw <= f * mag:
            return f * mag
    return 10 * mag


def render(vcd, path=None, start_ns=None, span_ns=None, show_all=False, all_signals=False, width=1200):
    entries, trig_ps, lines = runs(vcd)
    end_ns = vcd["end_ps"] / 1000.0
    trig_ns = trig_ps / 1000.0 if trig_ps is not None else None
    if show_all:
        t0, t1 = 0.0, end_ns
    else:
        t0 = start_ns if start_ns is not None else max(0.0, (trig_ns or 0.0) - 500.0)
        t1 = min(end_ns, t0 + (span_ns or 3000.0))
    if t1 <= t0:
        t1 = t0 + 1
    # cycles: amiga.cycles works in "ns" at a given MHz; with ps durations, MHz = 1e6
    cyc = amiga.cycles(entries, fabric_mhz=1e6)
    region = region_of(vcd, path)
    vectors = [s for s in ("ADDR",) if s in vcd["signals"]]
    sigs = [s for s in vcd["signals"] if s != "TRIGGER" and s not in ("ADDR", "SIZ", "RW", "REGION")]
    if not all_signals:
        def changes_in_view(s):
            return any(t0 * 1000 < t < t1 * 1000 for t, _ in lines[s][1:])
        sigs = [s for s in sigs if s in KEY_SIGNALS or changes_in_view(s)]
    left, right, top, row = 92, 16, 58, 24
    plot_w = width - left - right
    rows = vectors + sigs
    height = top + 28 + row * len(rows) + 34
    x = lambda t_ns: left + (t_ns - t0) / (t1 - t0) * plot_w
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
           f'viewBox="0 0 {width} {height}"><style>{CSS}</style>',
           f'<rect width="{width}" height="{height}" fill="var(--bg)"/>']
    title = html.escape(Path(path).name if path else "waveform")
    out.append(f'<text class="lbl" x="{left}" y="18" font-weight="600">{title}</text>')
    out.append(f'<text class="mut" x="{left}" y="34">{html.escape(vcd["comment"])}</text>')
    # time grid
    step = nice_step(t1 - t0)
    g = math.ceil(t0 / step) * step
    ybot = top + 28 + row * len(rows)
    while g <= t1 + 1e-9:
        xx = x(g)
        out.append(f'<line class="grid" x1="{xx:.1f}" y1="{top}" x2="{xx:.1f}" y2="{ybot}"/>')
        out.append(f'<text class="mut" x="{xx:.1f}" y="{ybot + 16}" text-anchor="middle">{g:g}</text>')
        g += step
    out.append(f'<text class="mut" x="{left + plot_w}" y="{ybot + 30}" text-anchor="end">ns</text>')
    # cycle bands + labels (row 0 is the cycle row)
    for k, c in enumerate(cyc):
        cs = c["t"]                       # ns (ps durations at "1e6 MHz")
        ce = cs + c["as_width"]
        if ce < t0 or cs > t1:
            continue
        xs, xe = x(max(cs, t0)), x(min(ce, t1))
        is_trig = trig_ns is not None and abs(cs - trig_ns) < 0.5
        cls = "cyc2" if is_trig else "cyc"
        band = len(out)
        out.append("")                    # the band, written once its label is known
        for xx in (x(cs), x(ce)):
            if left <= xx <= left + plot_w:
                out.append(f'<line class="edge" x1="{xx:.1f}" y1="{top}" x2="{xx:.1f}" y2="{ybot}"/>')
        rw = "R" if c["rw"] == "read" else "W"
        if "ds_to_dsack" in c:
            t = f"DS→DSACK {c['ds_to_dsack']:.0f} ns"
        elif "as_to_sterm" in c:
            t = f"STERM {c['as_to_sterm']:.0f} ns"
        else:
            t = ""
        logged = log_label(lines, round(cs * 1000))
        if logged:
            label = logged + (", " + t if t else "") + (", stale DSACK" if c["stale_dsack"] else "")
        else:
            label = " ".join(p for p in ((region if is_trig and region else ""), rw, t,
                                          "stale DSACK" if c["stale_dsack"] else "") if p)
        out[band] = (f'<rect class="{cls}" x="{xs:.1f}" y="{top}" width="{max(1.0, xe - xs):.1f}" '
                     f'height="{ybot - top}"><title>{html.escape(label)} (AS20 {cs:.0f}-{ce:.0f} ns)</title></rect>')
        short = fit_label(label, xe - xs)
        if short:
            out.append(f'<text class="lbl" x="{(xs + xe) / 2:.1f}" y="{top + 16}" text-anchor="middle">'
                       f'{html.escape(short)}</text>')
    out.append(f'<text class="mut" x="8" y="{top + 16}">cycles</text>')
    # address bus row (from the log): one segment per value, hex text where it fits
    for i, s in enumerate(vectors):
        y0 = top + 28 + row * i
        out.append(f'<text class="lbl" x="8" y="{y0 + row / 2 + 4:.1f}">{html.escape(s)}</text>')
        segs = [(t / 1000.0, v) for t, v in lines[s]]
        for k, (ts, v) in enumerate(segs):
            te = segs[k + 1][0] if k + 1 < len(segs) else end_ns
            if v is None or te <= t0 or ts >= t1:
                continue
            xs, xe = x(max(ts, t0)), x(min(te, t1))
            out.append(f'<rect class="bus" x="{xs:.1f}" y="{y0 + 4}" width="{max(1.0, xe - xs):.1f}" '
                       f'height="{row - 8}"><title>${v:08X}</title></rect>')
            if xe - xs > 70:
                out.append(f'<text class="lbl" x="{(xs + xe) / 2:.1f}" y="{y0 + row / 2 + 4:.1f}" '
                           f'text-anchor="middle" font-size="11">${v:08X}</text>')
    # signal rows
    for i, s in enumerate(sigs, start=len(vectors)):
        y0 = top + 28 + row * i
        hi, lo = y0 + 5, y0 + row - 5
        out.append(f'<text class="lbl" x="8" y="{y0 + row / 2 + 4:.1f}">{html.escape(s)}</text>')
        level = 1
        for t, v in lines[s]:
            if t / 1000.0 <= t0:
                level = v
        pts = [(x(t0), hi if level else lo)]
        for t, v in lines[s]:
            tn = t / 1000.0
            if t0 < tn < t1 and v != level:
                xx = x(tn)
                pts.append((xx, hi if level else lo))
                pts.append((xx, hi if v else lo))
                level = v
        pts.append((x(t1), hi if level else lo))
        out.append('<polyline class="sig" points="' + " ".join(f"{a:.1f},{b:.1f}" for a, b in pts) + '"/>')
    if trig_ns is not None and t0 <= trig_ns <= t1:
        xx = x(trig_ns)
        out.append(f'<line class="trig" x1="{xx:.1f}" y1="{top - 6}" x2="{xx:.1f}" y2="{ybot}"/>')
        out.append(f'<text class="tt" x="{xx + 4:.1f}" y="{top - 8}">trigger</text>')
    out.append("</svg>")
    return "\n".join(out) + "\n", {"cycles": len(cyc), "start_ns": t0, "end_ns": t1, "trigger_ns": trig_ns,
                                    "region": region, "signals": sigs,
                                    "address_log": bool(vectors)}


def convert(path, out=None, **kw):
    path = Path(path)
    svg, meta = render(parse_vcd(path.read_text()), path, **kw)
    out = Path(out) if out else path.with_suffix(".svg")
    if out.is_dir():
        out = out / path.with_suffix(".svg").name
    out.write_text(svg)
    meta["file"] = str(out)
    return meta


def add_args(p):
    p.add_argument("vcd", nargs="+", type=Path)
    p.add_argument("--out", type=Path, help="output file (one input) or directory (default: next to the VCD)")
    p.add_argument("--start-ns", type=float)
    p.add_argument("--span-ns", type=float)
    p.add_argument("--all", action="store_true", help="the whole capture")
    p.add_argument("--all-signals", action="store_true", help="also signals that do not change in view")
    return p


def run(args):
    out = []
    for v in args.vcd:
        o = args.out if (args.out and (len(args.vcd) == 1 or args.out.is_dir())) else None
        out.append(convert(v, o, start_ns=args.start_ns, span_ns=args.span_ns, show_all=args.all,
                           all_signals=args.all_signals))
    return out


def main(argv=None):
    a = add_args(argparse.ArgumentParser(prog="tfdiag.vcdsvg", description=__doc__,
                                         formatter_class=argparse.RawDescriptionHelpFormatter)).parse_args(argv)
    for m in run(a):
        print(m["file"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
