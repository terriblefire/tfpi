"""Clock counters (SPI 0xA4) -> frequencies.

Two reads a Pi-timed interval apart: the free-running fabric clk count gives
the PLL clock against Pi time (about 0.1% over 1 s; checked to 1%); the CLKAUX
count over the fixed gate window gives CLKAUX relative to the fabric clock, and
the fabric/CLKAUX ratio checks the PLL multiplier exactly (same oscillator).
"""

import time

from . import protocol as P


def measure(spi, interval=1.0, sleep=time.sleep, now=time.monotonic):
    a = spi.clocks_latch()
    t0 = now()
    sleep(interval)
    b = spi.clocks_latch()
    dt = now() - t0
    res = {"interval_s": round(dt, 4), "first": a, "second": b, "clocks": []}
    out = res["clocks"]
    gate = b.get("gate", 0)
    dfree = (b.get("free", 0) - a.get("free", 0)) & 0xFFFFFFFF
    if dfree == 0 or dt <= 0:
        out.append({"name": "fabric", "status": "finding", "mhz": None,
                    "msg": "the free-running fabric counter does not move: the PLL clock is not running"})
        return res
    fab = dfree / dt / 1e6
    err = fab / P.FABRIC_MHZ - 1
    ok = abs(err) <= P.FABRIC_TOLERANCE
    out.append({"name": "fabric", "mhz": round(fab, 3), "nominal": P.FABRIC_MHZ, "error_pct": round(100 * err, 2),
                "status": "info" if ok else "finding",
                "msg": (f"FPGA PLL clock {fab:.2f} MHz by Pi time ({100 * err:+.2f}%, tolerance +/-"
                        f"{100 * P.FABRIC_TOLERANCE:.0f}%)" +
                        ("" if ok else ": the PLL or its 25 MHz reference (OSC2) is off frequency"))})
    for name, nominal, what in P.CLOCKS:
        c = b.get(name, 0)
        rec = {"name": name, "nominal": nominal, "count": c, "gate": gate}
        if not gate or c == 0:
            rec.update(mhz=0.0, status="finding" if nominal else "info",
                       msg=f"{name} ({what}): no edges counted" + (": clock missing" if nominal else ""))
            out.append(rec)
            continue
        mhz = c / gate * fab                          # against Pi time
        rec["mhz"] = round(mhz, 3)
        if nominal:
            e = mhz / nominal - 1
            ok = abs(e) <= P.FABRIC_TOLERANCE
            rec.update(error_pct=round(100 * e, 2), status="info" if ok else "finding",
                       msg=f"{name} ({what}) {mhz:.3f} MHz vs {nominal} MHz ({100 * e:+.2f}%)")
        else:
            rec.update(status="info", msg=f"{name} ({what}) {mhz:.3f} MHz")
        out.append(rec)
        if name == "CLKAUX" and nominal:
            ratio = gate / c                          # fabric clks per CLKAUX period
            want = P.FABRIC_MHZ / nominal
            ok = abs(ratio / want - 1) <= 0.001
            out.append({"name": "pll_ratio", "ratio": round(ratio, 5), "nominal": want,
                        "status": "info" if ok else "finding",
                        "msg": f"fabric clock / CLKAUX = {ratio:.4f} (expected {want:g}: the PLL multiplier)"
                               + ("" if ok else ": the PLL is not locked or runs at the wrong ratio")})
    return res
