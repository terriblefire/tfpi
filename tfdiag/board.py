"""Board test: CPLD JTAG chain, SAMPLE snapshot, and the EXTEST interconnect test
with the pin readback image (diag_pins.bin) in the RAM FPGA.

Sequence:
 1. chain check (IDCODE, USERCODE, BYPASS, boundary length) and a SAMPLE snapshot
    with the normal image running (a314d stopped);
 2. load diag_timing.bin and hold the Amiga in reset (0xA1): RESET -> IC10 ->
    CPU_RSTI low, so the 060 floats its bus;
 3. load diag_pins.bin (every FPGA pin an input; CPU_RSTI stays low because IC10
    is clocked by PCLK_CPLD, which stops); SAMPLE again to confirm;
 4. EXTEST walking 0/1 over every net nobody else drives, reading back every
    CPLD pin and every FPGA pin; ends with a JTAG test-logic reset.
The FPGA is left with diag_pins.bin loaded: restore brings the flash image back.
"""

import json
from pathlib import Path

from . import jtag as J
from .hooks import checkpoint


def _w(path, data):
    Path(path).write_text(json.dumps(data, indent=1, default=str))


def run_board(cpld, spi, fpga, events, out_dir, images, info=None, extra_dnd=()):
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    info = info or {}
    shas = info.get("images") or {}
    res = {}
    ev = events
    ev.step("jtag", "CPLD JTAG chain")
    ev.progress(5, "JTAG chain check")
    chain = cpld.chain_check()
    res["chain"] = chain
    if not chain.get("idcode_ok"):
        ev.result("cpld_idcode", "finding", (chain.get("error") or "no IDCODE") +
                  ": check the Pi header JTAG wiring, the CPLD's 3.3 V supply, and that the board is powered")
        _w(out / "board.json", res)
        return res
    ev.result("cpld_idcode", "info", f"XC95288XL IDCODE {chain['idcode']}, usercode {chain.get('usercode')}")
    ev.result("cpld_bypass", "info" if chain.get("bypass_ok") else "finding",
              "BYPASS register is 1 bit" if chain.get("bypass_ok") else
              "BYPASS did not read back as 1 bit: the TDI/TDO path looks broken or noisy")
    bl = chain.get("boundary_length")
    ev.result("cpld_boundary_length", "info" if bl == 864 else "finding",
              f"boundary register {bl} bits (expected 864)")
    if not chain.get("ok"):
        _w(out / "board.json", res)
        return res

    ev.step("sample", "SAMPLE snapshot")
    ev.progress(15, "SAMPLE snapshot")
    rows = cpld.sample(16)
    res["sample_start"] = cpld.decode(rows)
    cpld.tlr()
    checkpoint("board.timing_image")

    # diag_pins.bin holds RESET low itself (and clocks IC10 so CPU_RSTI follows): no 0xA1
    ev.step("pins", "loading the pin readback image")
    ev.progress(35, "diag_pins.bin")
    fpga.load_image(Path(images) / "diag_pins.bin", shas.get("diag_pins.bin"))
    order = None
    pj = Path(images) / "diag_pins.json"
    if pj.exists():
        try:
            m = json.loads(pj.read_text())
            order = [str(x[1]) if isinstance(x, list) else str(x) for x in m]
        except ValueError:
            order = None
    checkpoint("board.pins_image")
    fread = lambda: spi.pinread(order)
    first = fread()
    if first is None:
        ev.result("pinread", "incomplete", "the pin readback image does not answer: FPGA pins not checked")
    rows = cpld.sample(8)
    rsti = [cpld.level(v, "CPU_RSTI") for v in rows]
    in_reset = all(x == 0 for x in rsti)
    res["sample_pins"] = cpld.decode(rows)
    res["cpu_in_reset"] = in_reset
    cpld.tlr()
    if not in_reset:
        ev.warn("CPU_RSTI is not low: the 060 may be driving its bus; 060-side nets are not driven")

    ev.step("extest", "EXTEST interconnect")
    pid = spi.diag_id() if first is not None else None
    parked = bool(pid and "sdram_parked" in pid["features"])
    res["pins_image_id"] = pid
    res["sdram_parked"] = parked
    if first is not None and not parked:
        ev.info("the pin readback image does not park the SDRAMs (diag ID bit 7): "
                "nets shared with the SDRAMs are only observed")
    targets, skipped = cpld.drivable(in_reset, first is not None, extra_dnd, sdram_parked=parked)
    res["skipped"] = skipped
    ev.info(f"driving {len(targets)} nets; {len(skipped)} left alone (other drivers / do-not-drive)")

    def prog(i, n, name):
        ev.progress(40 + 55 * i / max(1, n), f"EXTEST {name}")
        if i == n // 2:
            checkpoint("board.extest_mid")

    steps = cpld.extest(targets, fread if first is not None else None, prog)
    an = cpld.analyse(steps, targets)
    res["extest"] = an
    res["extest_raw"] = [{**s, "cpld": f"{s['cpld']:x}"} for s in steps]
    for rec in an["nets"]:
        ev.result(f"net_{rec['name']}", rec["status"], rec["msg"], step="extest")
    if an["unstable"] or an["unstable_fpga_pins"]:
        ev.result("float_recheck", "info", "toggling/floating while the Amiga is held in reset (not used "
                  "as followers): " + ", ".join(an["unstable"])
                  + (("; FPGA pins " + ", ".join(an["unstable_fpga_pins"])) if an["unstable_fpga_pins"] else ""),
                  step="extest")
    nfail = sum(1 for r in an["nets"] if r["status"] == "finding")
    ev.result("extest", "finding" if nfail else "info",
              f"{len(an['nets'])} nets driven, {nfail} with findings" if nfail else
              f"{len(an['nets'])} nets driven 0 and 1: no shorts, no opens", step="extest")
    _w(out / "board.json", res)
    return res
