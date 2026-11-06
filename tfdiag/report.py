"""report.html + report.txt from a result directory:

  DIR/before.json, DIR/board/board.json, DIR/timing/timing.json, DIR/restore.json

Organised per Amiga region and the chips that answer there (amiga.REGIONS),
machine ID first: the timing capture characterises the motherboard chips, not
the TF firmware. Timing numbers are measurements only (no pass/fail: good or bad
is decided at home base from captures of many machines). The board test and the
exerciser list what they saw as "finding" rows, worded as observations; parts
that could not be captured are "incomplete"; the restore check is the one real
check ("restored" / "differs"). Each row has one line on what it means.
"""

import html
import json
import time
from pathlib import Path

from . import __version__
from . import amiga
from . import protocol as P
from . import timing as T

MEANING = {
    10: "DSACK still asserted from the previous cycle when AS20 falls (stale DSACK): the CPU can end "
        "the new cycle at once (seen on some A3000/A4000 at high bus clocks).",
    11: "AS20 started and then cut short because INTCYCLE blocked it (TF card side).",
    6: "Read data still changing after DSACK (the CPU samples about one clock after DSACK).",
    15: "Write data changing after DS20 fell (the chipset latches on DS20).",
    0: "Delay from the 060 starting a cycle (TS) to the Amiga-side AS20.",
    1: "Delay from the FPGA releasing INTCYCLE to AS20: how quickly the CPLD starts an Amiga cycle.",
    2: "How long AS20 is held: the length of each Amiga bus cycle.",
    3: "Read access time of the Amiga target (DS20 to DSACK).",
    4: "Write access time of the Amiga target (DS20 to DSACK).",
    5: "How long the target keeps DSACK after AS20 ends.",
    7: "CPLD latency from DSACK to TA_BUS (the 060's cycle end).",
    8: "Time from TA_BUS to the end of AS20.",
    9: "Idle time between Amiga bus cycles.",
    12: "How long the target keeps DSACK after DS20 ends.",
    13: "Full external cycle time seen by the 060 (TS to TA_BUS).",
    14: "Write data changes after AS20 fell (normal up to the data setup).",
}
STATUS_ORDER = {"info": 0, "pass": 0, "finding": 1, "incomplete": 1, "fail": 2}
# what the report shows in its first column
SHOWN = {"info": "", "finding": "finding", "incomplete": "incomplete", "pass": "restored", "fail": "differs"}


def _load(path):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None


def collect(d):
    d = Path(d)
    data = {"before": _load(d / "before.json"), "board": _load(d / "board" / "board.json"),
            "timing": _load(d / "timing" / "timing.json"), "restore": _load(d / "restore.json"),
            "info": None}
    return data


TF_MEAS = {1, 11, 23, 24}     # TF card side (INTCYCLE): info only
HIST_MEANING = [
    "Read access time the chip gives the CPU: data strobe to acknowledge.",
    "Write access time: data strobe to acknowledge.",
    "Synchronous termination (Ramsey fast RAM): address strobe to the cycle end, no DSACK.",
    "Whole asynchronous cycle: address strobe to the TF card ending the 060's cycle.",
    "When the read data stops changing after the data strobe (D0-D7 only).",
    "How fast the TF card ends the 060's cycle after the chip's DSACK.",
    "How long the chip keeps DSACK after the cycle ends.",
    "DSACK still asserted from the previous cycle when AS20 falls (stale DSACK): time until it lets go.",
]
CYCLE_KEYS = ("as_to_dsack", "ds_to_dsack", "as_to_sterm", "as_width", "dsack_release")


def _cycle_ns(c, mhz):
    """A waveform cycle (fabric clks) in ns at the preset's fabric clock."""
    out = dict(c)
    for k in CYCLE_KEYS:
        if k in out:
            out[k] = amiga.to_ns(out[k], mhz)
    return out


def _amiga_hist_rows(add, sec, ah, presets, reg, mhz):
    """The 0xA8 histograms of one region: one row per measurement, one value per preset."""
    per = {p: (ah.get(str(p)) or {}).get(reg) for p in presets}
    per = {p: v for p, v in per.items() if v}
    if not per:
        return
    add(sec, "Cycles binned (histograms)", "; ".join(
        f"p{p}: {v['as']} AS20 in {v['windows']} windows, {v['seconds']:g} s"
        + (" (exerciser held in this group)" if v.get("held") else "")
        + (f", {v['lost']} samples lost" if v["lost"] else "")
        for p, v in per.items()), "", "info",
        "AS20 cycles in this region while the histogram windows ran (short windows summed on the Pi, so "
        "the 16-bit counters do not fill); lost = samples dropped while the previous one was being stored.")
    for m, (name, special) in enumerate(P.HIST_MEAS):
        cells = []
        for p, v in per.items():
            sm = amiga.hist_summary(m, v["bins"][m], v["shift"], mhz(p))
            if not sm["n"] and not sm.get("bin0"):
                continue
            if sm["n"]:
                txt = (f"median {sm['median']} ns ({sm['min']}..{'>=' if sm['open'] else ''}{sm['max']}, "
                       f"n={sm['n']})")
            else:
                txt = "none"
            if special and sm.get("bin0"):
                txt += f", {sm['bin0']} {special}"
            if v.get("saturated_windows"):
                # a 16-bit counter hit 65535 within one short window: that window's counts are low
                txt += f" (a counter saturated in {v['saturated_windows']} of {v['windows']} windows)"
            cells.append(f"p{p}: {txt}")
        if cells:
            add(sec, name + " (histogram)", "; ".join(cells), "", "info",
                HIST_MEANING[m] + f" Bins of {1 << next(iter(per.values()))['shift']} fabric clk(s); "
                "values are bin lower edges.")


def _hist_cell(meas, rec):
    """(value text, status, limit, extra meaning) for one preset's histogram, or None if empty."""
    st = T.hist_stats(meas, rec["bins"])
    if P.MEAS[meas][1] is None:
        if not st["count"]:
            return None
        peak = max(range(len(st["phase"])), key=lambda i: st["phase"][i])
        return f"peak +{st['phase_ns'][peak]} ns", "info", "", ""
    if not st["count"] and not st.get("special"):
        return None
    if st["count"]:
        top = f">={st['max_lo_ns']}" if st.get("max_open") else st["max_ns"]
        val = f"{st['min_ns']}..{top} ns (n={st['count']})"
    else:
        val = "-"
    if st.get("special"):
        val += f" +{st['special']} special"
    extra = ""
    if st.get("max_open"):
        extra += " Slow cycles run past the histogram range: see the waveform numbers."
    if rec.get("saturated"):
        extra += " Counters saturated: counts are lower bounds."
    return val, "info", "", extra


def _hist_rows(add, sec, hist, presets, reg, keep):
    metas = sorted({int(m) for p in presets for m in ((hist.get(str(p)) or {}).get(reg) or {})})
    for meas in metas:
        if not keep(meas):
            continue
        cells = []
        for p in presets:
            rec = ((hist.get(str(p)) or {}).get(reg) or {}).get(str(meas))
            c = _hist_cell(meas, rec) if rec else None
            if c:
                cells.append((p, c))
        if not cells:
            continue
        val = "; ".join(f"p{p}: {c[0]}" for p, c in cells)
        meaning = MEANING.get(meas, "") + "".join(sorted({c[3] for _, c in cells}))
        add(sec, P.MEAS[meas][0] + " (tprobe)", val, "", "info", meaning)


def machine_rows(add, m, mtype):
    if not m:
        add("Machine", "Machine ID", "not read", "", "info",
            "The exerciser did not report the machine (it did not start, or the timing test was not run).")
        return
    alice = (m.get("vposr", 0) >> 8) & 0x7F
    add("Machine", "Machine type", mtype or "unknown", "", "info",
        "From the Kickstart ROM model (3 = A3000, 4 = A4000), or --machine.")
    add("Machine", "Ramsey", f"version 0x{m.get('ramsey_version', 0):02x}, control 0x{m.get('ramsey_ctl', 0):02x}",
        "", "info", "Ramsey: motherboard fast RAM controller (0xFF = not found).")
    add("Machine", "Gary / Fat Gary", f"ID 0x{m.get('gary_id', 0):02x}", "", "info", "Gary/Fat Gary bus controller.")
    add("Machine", "Alice / Agnus", f"ID 0x{alice:02x} (VPOSR 0x{m.get('vposr', 0):04x})", "", "info",
        "Chip RAM and custom chip bus arbiter.")
    add("Machine", "Denise / Lisa", f"ID 0x{m.get('denise_id1', 0):04x}", "", "info", "Video chip ID.")
    add("Machine", "Chip RAM", f"{m.get('chip_ram_bytes', 0) // 1024} KB", "", "info", "Chip RAM found by the exerciser.")
    add("Machine", "Motherboard fast RAM", f"{m.get('mb_fast_bytes', 0) // (1024 * 1024)} MB at "
        f"0x{m.get('mb_fast_base', 0):08x}", "", "info", "Ramsey fast RAM found by the exerciser.")
    add("Machine", "Kickstart", f"{m.get('kick_version', 0)}.{m.get('kick_revision', 0)}"
        + ("" if m.get("kick_checksum") == 0xFFFFFFFF else " (ROM checksum bad)"), "", "info",
        "Kickstart version read from the ROM.")
    add("Machine", "68060 PCR", f"0x{m.get('pcr_060', 0):08x}", "", "info", "CPU revision register.")


def rows_for(data, machine_override=None):
    """[(section, name, value, limit, status, meaning)]"""
    rows = []
    add = lambda *r: rows.append(r)
    tm = data.get("timing") or {}
    m = tm.get("machine")
    mtype = amiga.machine_type(m, machine_override)
    machine_rows(add, m, mtype)

    # ---- per Amiga region / chip ------------------------------------------------------
    hist = tm.get("tprobe_histograms") or {}
    ah = tm.get("histograms") or {}
    cyc = tm.get("cycles") or {}
    fab = tm.get("fabric_mhz") or {}
    presets = sorted({int(p) for p in list(hist) + list(cyc) + list(ah)})
    mhz = lambda p: fab.get(str(p)) or P.FABRIC_MHZ
    if presets:
        add("Machine", "Clock presets measured", ", ".join(str(p) for p in presets), "", "info",
            "CLKSEL presets the exerciser ran; values below are per preset (p<n>).")
        add("Machine", "Fabric clock per preset", "; ".join(
            f"p{p}: " + (f"{fab[str(p)]} MHz" if fab.get(str(p)) else f"~{P.FABRIC_MHZ:g} MHz (not measured)")
            for p in presets), "", "info",
            "The diag timers count fabric clks (the PLL output at the running preset); this converts them to ns.")
    for reg in amiga.REGION_ORDER:
        title, chips, what = amiga.REGIONS[reg]
        sec = f"{title}: {chips}"
        _amiga_hist_rows(add, sec, ah, presets, reg, mhz)
        per = {}
        for p in presets:
            cl = [_cycle_ns(c, mhz(p)) for c in ((cyc.get(str(p)) or {}).get(reg) or [])]
            per[p] = amiga.region_table({reg: cl}).get(reg, {})
        keys = []
        for t in per.values():
            for k in t:
                if k not in keys:
                    keys.append(k)
        order = list(amiga.METRICS) + ["stale_dsack"]
        keys.sort(key=lambda k: (order.index(k[0]), k[1]))
        for metric, rw in keys:
            cells = [(p, per[p][(metric, rw)]) for p in presets if (metric, rw) in per[p]]
            if metric == "stale_dsack":
                cells = [(p, c) for p, c in cells if c["n"]]
                if not cells:
                    continue
                add(sec, "Stale DSACK (waveform)", "; ".join(f"p{p}: {c['count']}/{c['n']}" for p, c in cells),
                    "", "info", "Cycles where DSACK was still asserted from the previous cycle when AS20 fell "
                    "(count / cycles captured).")
                continue
            label, meaning = amiga.METRICS[metric]
            val = "; ".join(f"p{p}: " + (f"{c['median']} ns" if c["n"] == 1 else
                                         f"{c['min']}..{c['max']} ns, median {c['median']} (n={c['n']})")
                            for p, c in cells)
            add(sec, f"{label}, {rw}s (waveform)", val, "", "info", f"{meaning} {what[0].upper() + what[1:]}.")
        _hist_rows(add, sec, hist, presets, reg, lambda m: True)        # tprobe images only
    _hist_rows(add, "All Amiga bus cycles", hist, presets, "all", lambda m: m not in TF_MEAS)
    cc = tm.get("cycle_counters") or {}
    if cc:
        ps = sorted(cc, key=int)
        rng = lambda c, a, b, p: ("none" if c[a] == 0xFF and c[b] == 0 else
                                  f"{amiga.to_ns(c[a], mhz(p)):g}..{amiga.to_ns(c[b], mhz(p)):g} ns")
        add("All Amiga bus cycles", "Cycles counted (cycle counters)",
            "; ".join(f"p{p}: {cc[p]['as']} AS20, {cc[p]['dsack']} DSACK, {cc[p]['sterm']} STERM" for p in ps),
            "", "info", "How the Amiga ended the bus cycles while the exerciser ran: DSACK (asynchronous: "
            "Gary, Alice, CIAs, ROM) or STERM (synchronous: Ramsey fast RAM).")
        add("Motherboard fast RAM: Ramsey",
            "AS20 -> STERM (cycle counters)", "; ".join(f"p{p}: {rng(cc[p], 'sterm_min', 'sterm_max', p)}" for p in ps),
            "", "info", "Ramsey's synchronous termination latency over every STERM cycle (min..max).")
        add("TF card bus interface", "DSACK -> TA_BUS (cycle counters)",
            "; ".join(f"p{p}: {rng(cc[p], 'dsack_ta_min', 'dsack_ta_max', p)}, {cc[p]['fast']} of {cc[p]['dsack']} "
                      f"within {amiga.to_ns(cc[p].get('threshold', 0), mhz(p)):g} ns" for p in ps), "", "info",
            "How fast the TF card ends the 060's cycle after the Amiga's DSACK.")
    _hist_rows(add, "TF card bus interface", hist, presets, "all", lambda m: m in TF_MEAS)

    # ---- exerciser -------------------------------------------------------------------------
    for h in tm.get("hangs", []):
        add("Exerciser", f"preset {h['preset']}: hang in {h.get('group', h['phase'])}", h.get("why", "hang"),
            "", "finding", "The Amiga stopped at this clock preset; it was reset and the run resumed at "
            "the next preset.")
    for r in tm.get("results", []):
        if not r.get("failure", True):
            add("Exerciser", f"preset {r['preset']}: {r['group']}", f"{r['flags']} {r['addr']} {r['got']}", "",
                "info", "Exerciser note (not a failure).")
            continue
        add("Exerciser", f"preset {r['preset']}: {r['group']} @ {r['addr']}", f"got {r['got']}",
            f"expected {r['expected']}", "finding", f"{r['size']}-byte access read back different data"
            + (" (exception)" if "exception" in r.get("flags", []) else "") + ".")
    if tm and tm.get("ok") and m is not None and not any(r.get("failure", True) for r in tm.get("results", [])):
        add("Exerciser", "data read back", "as written", "", "info",
            "No different data read back in the tests that ran" + (" (see the hangs above)." if tm.get("hangs") else "."))

    # ---- board test ------------------------------------------------------------------------
    bd = data.get("board") or {}
    ch = bd.get("chain") or {}
    if ch:
        add("Board", "JTAG IDCODE", ch.get("idcode"), "XC95288XL", "info" if ch.get("idcode_ok") else "finding",
            "What the CPLD answers on the JTAG chain.")
        if "bypass_ok" in ch:
            add("Board", "JTAG BYPASS", "1 bit" if ch["bypass_ok"] else "not 1 bit", "1 bit", "info" if ch["bypass_ok"]
                else "finding", "TDI to TDO path through the CPLD.")
        if "boundary_length" in ch:
            add("Board", "Boundary register", ch["boundary_length"], 864, "info" if ch["boundary_length"] == 864
                else "finding", "Length of the CPLD boundary-scan register.")
    ex = bd.get("extest") or {}
    for n in ex.get("nets", []):
        if n["status"] != "info":
            add("Board", f"net {n['name']}", "seen", "drives 0/1, nobody follows", n["status"], n["msg"])
    if ex.get("nets"):
        plain = sum(1 for n in ex["nets"] if n["status"] == "info")
        add("Board", "Interconnect", f"{plain}/{len(ex['nets'])} nets follow 0/1 with nothing else following",
            "", "info", "Nets driven by the CPLD in EXTEST and read back by the CPLD and the FPGA.")
    if bd.get("skipped"):
        add("Board", "Nets not driven", len(bd["skipped"]), "", "info",
            "Nets with another driver (Amiga slot, bus switches, buffers) are only observed.")

    # ---- TF card identity / build ---------------------------------------------------------------
    b = data.get("before") or {}
    if b:
        add("TF card", "CPLD IDCODE / usercode", f"{b.get('cpld_idcode')} / {b.get('cpld_usercode')}", "", "info"
            if b.get("cpld_idcode") else "incomplete", "The BUS CPLD on JTAG; the usercode names its design.")
        add("TF card", "SPI flash", f"{b.get('flash_jedec')} sha256 {(b.get('flash_sha256') or '')[:16]}", "",
            "info" if b.get("flash_jedec") else "incomplete", "The FPGA's flash and a checksum of the installed image.")
    did = tm.get("diag_id")
    if did:
        add("TF card", "Diag image", f"v{did['version']} build {did['commit']}", "", "info",
            "Diagnostic FPGA build (features: " + ", ".join(did.get("features", [])) + ").")
    for c in (tm.get("clocks") or {}).get("clocks", []):
        val = f"{c['mhz']} MHz" if c.get("mhz") is not None else (str(c.get("ratio")) if "ratio" in c else "-")
        lim = (f"{c['nominal']:g} +/-0.1%" if c["name"] == "pll_ratio" else
               f"{c['nominal']:g} MHz +/-1%" if c.get("nominal") else "")
        add("TF card", f"clock {c['name']}", val, lim, c["status"], c["msg"])
    sw = tm.get("clock_switches") or []
    if sw:
        add("TF card", "Clock switch (CLKSEL)", "; ".join(
            f"p{x['preset']}: " + (f"{x['polls']} polls, {x['bus_errors']} bus errors"
                                   + ("" if x.get("acked") is None else ", acked" if x["acked"] else ", not acked")
                                   if x["settled"]
                                   else "did not settle (timeout)") for x in sw), "", "info",
            "Per switch: reads of $E90044 until it showed the new preset 64 times in a row (how long the "
            "PLL took to switch), and the bus errors TF-side accesses took meanwhile. Acked: the Pi answered "
            "the switch announcement and kept SPI quiet while the FPGA ran on its bypass clock.")
    al = tm.get("address_log") or {}
    if tm:
        add("TF card", "Address log", (f"{al.get('captures', 0)} captures, {al.get('entries', 0)} cycles logged, "
                                       f"{al.get('aligned', 0)} lined up with the waveform, {al.get('lost', 0)} lost")
            if al.get("used") else "not used (the body never reported running, or an older image)", "",
            "info", "Per-cycle addresses (0xB4) recorded with each waveform capture; they label the cycles in "
            "the VCDs and give every captured cycle its region.")
    rs = data.get("restore") or {}
    if rs:
        add("Restore", "Restore check", "ok" if rs.get("ok") else "; ".join(rs.get("differences", [])),
            "as before", "pass" if rs.get("ok") else "fail", rs.get("advice", ""))
    first = {}
    for i, r in enumerate(rows):
        first.setdefault(r[0], i)
    return sorted(rows, key=lambda r: first[r[0]])        # stable: rows keep their order per section


def _hist_svg(bins):
    m = max(bins) or 1
    w = 4
    bars = "".join(f'<rect x="{i * w}" y="{20 - 20 * c / m:.1f}" width="{w - 1}" height="{20 * c / m:.1f}"/>'
                   for i, c in enumerate(bins))
    return f'<svg class="h" viewBox="0 0 {w * len(bins)} 20" width="{w * len(bins)}" height="20">{bars}</svg>'


NOTE = ("Measurements only: nothing here is judged good or bad on the Pi. Send the zip to us; "
        "\"finding\" rows are what the board test or the exerciser saw.")


def to_text(rows, meta):
    counts = meta["counts"]
    out = [f"TF4060 diagnostics capture  ({meta['generated']}, tfdiag {__version__})", NOTE,
           f"{counts.get('finding', 0)} findings, {counts.get('incomplete', 0)} incomplete", ""]
    sec = None
    for s, name, val, lim, status, meaning in rows:
        if s != sec:
            out += ["", f"== {s}"]
            sec = s
        mark = SHOWN.get(status, status)
        out.append(f"  {('[' + mark.upper() + ']') if mark else '':12s} {name}: {val}"
                   + (f"   (reference: {lim})" if lim else ""))
        if meaning:
            out.append(f"         {meaning}")
    return "\n".join(out) + "\n"


def to_html(rows, meta, data):
    esc = lambda x: html.escape("" if x is None else str(x))
    counts = meta["counts"]
    parts = [
        "<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>",
        "<title>TF4060 diagnostics</title><style>",
        ":root{--bg:#fff;--fg:#1d1d1f;--mut:#666;--line:#ddd;--pass:#1a7f37;--finding:#9a6700;--fail:#cf222e;--info:#57606a}",
        "@media (prefers-color-scheme:dark){:root{--bg:#111;--fg:#eee;--mut:#aaa;--line:#333;--pass:#3fb950;--finding:#d29922;--fail:#f85149;--info:#8b949e}}",
        "body{background:var(--bg);color:var(--fg);font:14px/1.4 system-ui,sans-serif;margin:0 auto;max-width:1100px;padding:16px}",
        "table{border-collapse:collapse;width:100%;margin:8px 0 24px}td,th{border-bottom:1px solid var(--line);padding:4px 6px;text-align:left;vertical-align:top}",
        ".s{font-weight:600;text-transform:uppercase;font-size:12px}.pass{color:var(--pass)}.finding,.incomplete{color:var(--finding)}.fail{color:var(--fail)}.info{color:var(--info)}",
        ".m{color:var(--mut);font-size:12px}svg.h rect{fill:currentColor}.wrap{overflow-x:auto}",
        "</style></head><body>",
        f"<h1>TF4060 diagnostics capture</h1><p class='m'>{esc(meta['generated'])} &middot; tfdiag {__version__} &middot; "
        f"<span class='finding'>{counts.get('finding', 0)} findings</span>, "
        f"<span class='incomplete'>{counts.get('incomplete', 0)} incomplete</span></p><p>{esc(NOTE)}</p>",
    ]
    sec = None
    for s, name, val, lim, status, meaning in rows:
        if s != sec:
            if sec is not None:
                parts.append("</table></div>")
            parts.append(f"<h2>{esc(s)}</h2><div class='wrap'><table><tr><th></th><th>Item</th><th>Value</th>"
                         "<th>Reference</th><th>What this is</th></tr>")
            sec = s
        parts.append(f"<tr><td class='s {status}'>{esc(SHOWN.get(status, status))}</td><td>{esc(name)}</td><td>{esc(val)}</td>"
                     f"<td>{esc(lim)}</td><td class='m'>{esc(meaning)}</td></tr>")
    if sec is not None:
        parts.append("</table></div>")
    # board view hook (tfdiag/boardview.py): findings on a drawing of the r5 board; "" without board results
    from . import boardview
    parts.append(boardview.html_section(data))
    tm = data.get("timing") or {}
    ah = tm.get("histograms") or {}
    if ah:
        parts.append("<h2>Histograms</h2><div class='wrap'><table><tr><th>Preset</th><th>Region</th>"
                     "<th>Measurement</th><th>Bin width</th><th>Bins 0-31</th></tr>")
        for preset, regs in sorted(ah.items(), key=lambda x: int(x[0])):
            for reg, v in regs.items():
                for m, (name, _) in enumerate(P.HIST_MEAS):
                    if sum(v["bins"][m]):
                        parts.append(f"<tr><td>{esc(preset)}</td><td>{esc(reg)}</td><td>{esc(name)}</td>"
                                     f"<td>{1 << v['shift']} clk</td><td>{_hist_svg(v['bins'][m])}</td></tr>")
        parts.append("</table></div><p class='m'>Bin counts are in timing/histograms.csv and .json.</p>")
    hist = tm.get("tprobe_histograms") or {}
    if hist:
        parts.append("<h2>tprobe histograms</h2><div class='wrap'><table><tr><th>Preset</th><th>Region</th>"
                     "<th>Measurement</th><th>Bins 0-31</th></tr>")
        for preset, regs in sorted(hist.items(), key=lambda x: int(x[0])):
            for reg, ms in regs.items():
                for meas, rec in sorted(ms.items(), key=lambda x: int(x[0])):
                    if not sum(rec["bins"]):
                        continue
                    parts.append(f"<tr><td>{esc(preset)}</td><td>{esc(reg)}</td><td>{esc(P.MEAS[int(meas)][0])}</td>"
                                 f"<td>{_hist_svg(rec['bins'])}</td></tr>")
        parts.append("</table></div>")
    if tm.get("waves"):
        parts.append("<h2>Waveforms</h2><ul>")
        for w in tm["waves"]:
            parts.append(f"<li>{esc(w['file'])}: preset {esc(w['preset'])}, {esc(w['trigger'])}"
                         f" ({esc(w.get('region_name', ''))}), {esc(w['entries'])} entries</li>")
        parts.append("</ul><p class='m'>Open the .vcd files with GTKWave or PulseView.</p>")
    parts.append("</body></html>")
    return "\n".join(parts)


def write(d, events=None, machine=None):
    d = Path(d)
    data = collect(d)
    rows = rows_for(data, machine)
    counts = {}
    for r in rows:
        counts[r[4]] = counts.get(r[4], 0) + 1
    meta = {"generated": time.strftime("%Y-%m-%d %H:%M:%S"), "counts": counts}
    (d / "report.txt").write_text(to_text(rows, meta))
    (d / "report.html").write_text(to_html(rows, meta, data))
    if events:
        events.info(f"report written: {counts}")
    return {"files": ["report.html", "report.txt"], "counts": counts, "rows": len(rows)}
