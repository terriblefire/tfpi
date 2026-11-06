"""Run-length waveform entries -> VCD.

entries: [(signals, duration_clks)] in time order: each entry is a signal state
and how many fabric clocks it lasted (protocol.WAVE_*).
events: optional [(j, log entry)] from the address log, aligned to the waveform:
at the start of entry j (an AS20 fall) the vectors ADDR[31:0], SIZ[1:0] (060 SIZ1/SIZ0),
RW (1 = read) and REGION[2:0] (histogram region code) take that cycle's values.
"""

import time

from . import protocol as P

_IDS = "!\"#$%&'()*+,-./:;<=>?@ABCDEFGHIJKLMNOPQRSTUVWXYZ"     # no digits: they read as values
LOG_VECTORS = [("ADDR", 32, "addr"), ("SIZ", 2, "siz"), ("RW", 1, "rw"), ("REGION", 3, "region")]


def _value(v, width, vid):
    """A VCD value change: scalar '1!' or vector 'b0101 !' (v None = unknown)."""
    if width == 1:
        return ("x" if v is None else str(v)) + vid
    return ("bx" if v is None else "b" + format(v, f"0{width}b")) + " " + vid


def to_vcd(entries, signals=None, fabric_mhz=None, title="tf4060 diag capture", trigger_index=None,
           events=None):
    signals = signals or P.WAVE_SIGNALS
    fabric_mhz = fabric_mhz or P.FABRIC_MHZ
    period_ps = 1e6 / fabric_mhz
    lines = [
        f"$date {time.strftime('%Y-%m-%d %H:%M:%S')} $end",
        "$version tfdiag $end",
        f"$comment {title}; 1 tick = 1 ps; fabric clock {fabric_mhz} MHz $end",
        "$timescale 1ps $end",
        "$scope module tf4060 $end",
    ]
    for i, name in enumerate(signals):
        lines.append(f"$var wire 1 {_IDS[i]} {name} $end")
    trig_id = _IDS[len(signals)]
    lines.append(f"$var wire 1 {trig_id} TRIGGER $end")
    ev = dict(events or [])
    vec_ids = {}
    if ev:
        for k, (name, width, _) in enumerate(LOG_VECTORS):
            vec_ids[name] = _IDS[len(signals) + 1 + k]
            lines.append(f"$var wire {width} {vec_ids[name]} {name} $end")
    lines += ["$upscope $end", "$enddefinitions $end"]
    t = 0
    prev = None
    last_vec = {}
    for k, (sig, dur) in enumerate(entries):
        ch = []
        for i in range(len(signals)):
            b = (sig >> i) & 1
            if prev is None or ((prev >> i) & 1) != b:
                ch.append(f"{b}{_IDS[i]}")
        if prev is None:
            ch.append(f"0{trig_id}")
            if ev:
                ch += [_value(None, width, vec_ids[name]) for name, width, _ in LOG_VECTORS]
        if trigger_index is not None and k == trigger_index:
            ch.append(f"1{trig_id}")
        if trigger_index is not None and k == trigger_index + 1:
            ch.append(f"0{trig_id}")
        if k in ev:
            for name, width, key in LOG_VECTORS:
                v = ev[k][key]
                if last_vec.get(name) != v:
                    ch.append(_value(v, width, vec_ids[name]))
                    last_vec[name] = v
        if ch:
            lines.append(f"#{round(t * period_ps)}")
            lines += ch
        prev = sig
        t += max(1, dur)
    lines.append(f"#{round(t * period_ps)}")
    return "\n".join(lines) + "\n"


def write(path, entries, **kw):
    with open(path, "w") as fh:
        fh.write(to_vcd(entries, **kw))
    return path
