"""How the Amiga motherboard chips answer bus cycles.

The timing capture characterises the Amiga side (Gary/Fat Gary, Ramsey,
Alice/Agnus, Buster, the CIAs, the Kickstart ROM), not the TF firmware. This
module holds:
  * REGIONS: tprobe/waveform region code -> which chips answer there;
  * cycles(): bus cycles (AS20 fall .. AS20 rise) decoded from a run-length
    waveform capture: AS->DSACK, DS->DSACK, AS->TA_BUS, STERM (TA_BUS with no
    DSACK), stale DSACK, AS width;
  * region_table(): per-region summaries (min/median/max) of those numbers.
Measurements only: whether a number is good or bad is decided at home base,
from captures of many machines, not on the Pi.
"""

from . import protocol as P

# region code (protocol.REGIONS) -> (title, chips, what the cycles are)
REGIONS = {
    "chip": ("Chip RAM", "Alice/Agnus via Gary/Fat Gary (and Buster)",
             "chip RAM 0x000000-0x1FFFFF: the cycle waits for a free chip bus slot"),
    "custom": ("Custom chip registers", "Alice/Agnus, Denise/Lisa, Paula via Gary/Fat Gary",
               "0xDFF000 registers: chip bus slot like chip RAM"),
    "cia": ("CIAs", "8520 CIA-A/CIA-B via Gary/Fat Gary",
            "0xBFxxxx: synchronised to the E clock (CPU clock / 10, about 709 kHz): slow and variable"),
    "rom": ("Kickstart ROM", "ROM via Gary/Fat Gary", "0xF80000-0xFFFFFF reads"),
    "ramsey": ("Motherboard fast RAM", "Ramsey",
               "0x04000000-0x07FFFFFF: Ramsey ends its cycles with STERM (synchronous), not DSACK"),
    "other": ("Everything else", "Buster (Zorro), other motherboard space",
              "addresses outside the regions above"),
}
REGION_ORDER = ["chip", "custom", "cia", "rom", "ramsey", "other"]
# histogram / waveform region codes (0xA8 / 0xB0 kind 3)
REGION_BY_CODE = {P.HIST_REGIONS[k]: k for k in REGION_ORDER}

# metric -> (label, what it means)
METRICS = {
    "as_to_dsack": ("AS20 -> DSACK", "Time from the address strobe until the chip acknowledges (DSACK)."),
    "ds_to_dsack": ("DS20 -> DSACK", "Access time seen by the CPU: data strobe to acknowledge."),
    "as_to_sterm": ("AS20 -> STERM", "Synchronous termination (Ramsey fast RAM): address strobe to STERM "
                                     "(seen as TA_BUS with no DSACK)."),
    "as_width": ("AS20 width", "Length of the whole bus cycle."),
    "dsack_release": ("AS20 rise -> DSACK release", "How long the chip keeps DSACK after the cycle ends."),
}

ROM_MODELS = {3: "A3000", 4: "A4000"}


def machine_type(machine, override=None):
    if override:
        return override
    if not machine:
        return None
    return ROM_MODELS.get(machine.get("rom_model"))


# ---- cycles from a waveform capture ----------------------------------------------------
def _bit(state, name):
    return (state >> P.WAVE_SIGNALS.index(name)) & 1


def timeline(entries, fabric_mhz=None):
    """[(t_ns, state)] at each run start."""
    ns = 1000.0 / (fabric_mhz or P.FABRIC_MHZ)
    t, out = 0.0, []
    for state, dur in entries:
        out.append((t, state))
        t += max(1, dur) * ns
    out.append((t, None))
    return out


def cycles(entries, fabric_mhz=None):
    """Complete AS20 cycles in a capture, in order."""
    tl = timeline(entries, fabric_mhz)
    out = []
    i = 1
    while i < len(tl) - 1:
        t, s = tl[i]
        prev = tl[i - 1][1]
        if _bit(prev, "AS20") == 1 and _bit(s, "AS20") == 0:
            cyc = {"t": round(t, 1), "rw": "read" if _bit(s, "RW20") else "write",
                   "stale_dsack": (_bit(s, "DSACK0") & _bit(s, "DSACK1")) == 0}
            t_ds = t_dk = t_ta = t_end = t_rel = None
            j = i
            dk_prev = 0 if cyc["stale_dsack"] else 1
            while j < len(tl) - 1:
                tj, sj = tl[j]
                if j > i and _bit(sj, "AS20") == 1:
                    t_end = tj
                    break
                dk = _bit(sj, "DSACK0") & _bit(sj, "DSACK1")
                if t_ds is None and _bit(sj, "DS20") == 0:
                    t_ds = tj
                if t_dk is None and dk == 0 and dk_prev == 1:
                    t_dk = tj
                dk_prev = dk
                if t_ta is None and _bit(sj, "TA_BUS") == 0:
                    t_ta = tj
                j += 1
            if t_end is None:
                break                       # capture ended inside the cycle
            # DSACK release after AS rise
            k = j
            while k < len(tl) - 1:
                tk, sk = tl[k]
                if _bit(sk, "DSACK0") & _bit(sk, "DSACK1"):
                    t_rel = tk
                    break
                k += 1
            cyc["as_width"] = round(t_end - t, 1)
            if t_dk is not None:
                cyc["as_to_dsack"] = round(t_dk - t, 1)
                if t_ds is not None and t_ds <= t_dk:
                    cyc["ds_to_dsack"] = round(t_dk - t_ds, 1)
            elif t_ta is not None:
                cyc["as_to_sterm"] = round(t_ta - t, 1)
            if t_rel is not None and t_dk is not None:
                cyc["dsack_release"] = round(t_rel - t_end, 1)
            out.append(cyc)
            i = j
        i += 1
    return out


def to_ns(clks, mhz):
    return round(clks * 1000.0 / (mhz or P.FABRIC_MHZ), 1)


def hist_summary(m, bins, shift, mhz=None):
    """One 0xA8 histogram (32 bins of 1 << shift fabric clks): sample count, bin-0
    count when bin 0 has its own meaning, and min / median / max in ns (bin lower
    edges; the last bin is open-ended)."""
    special = P.HIST_MEAS[m][1]
    first = 1 if special else 0
    counts = bins[first:]
    n = sum(counts)
    out = {"n": n, "shift": shift, "mhz": mhz or P.FABRIC_MHZ}
    if special:
        out["bin0"] = bins[0]
    if n:
        used = [first + i for i, c in enumerate(counts) if c]
        half, acc, med = n / 2.0, 0, used[0]
        for b in range(first, P.HIST_BINS):
            acc += bins[b]
            if acc >= half:
                med = b
                break
        out["min"] = to_ns(used[0] << shift, mhz)
        out["median"] = to_ns(med << shift, mhz)
        out["max"] = to_ns(used[-1] << shift, mhz)
        out["open"] = used[-1] == P.HIST_BINS - 1
    return out


def summarise(values):
    v = sorted(values)
    if not v:
        return None
    n = len(v)
    return {"n": n, "min": v[0], "median": v[n // 2], "max": v[-1]}


def region_table(cyc_by_region):
    """{region: {(metric, rw): summary}} from {region: [cycles]}."""
    out = {}
    for reg, cl in cyc_by_region.items():
        t = {}
        for m in METRICS:
            for rw in ("read", "write"):
                s = summarise([c[m] for c in cl if c["rw"] == rw and m in c])
                if s:
                    t[(m, rw)] = s
        stale = sum(1 for c in cl if c["stale_dsack"])
        t[("stale_dsack", "any")] = {"n": len(cl), "count": stale}
        out[reg] = t
    return out
