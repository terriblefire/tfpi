"""BUS CPLD (XC95288XL) boundary scan through openocd: chain check, SAMPLE
snapshot, EXTEST interconnect test, and the test-logic reset that ends every use.

Only the instructions in protocol.IR are ever shifted (EXTEST, SAMPLE/PRELOAD,
USERCODE, IDCODE, BYPASS): never ISP instructions, so the CPLD flash cannot be
touched. openocd runs with a generated config and is driven over its Tcl port.

EXTEST is only safe on nets nobody else drives. A net is driven only if every
member other than the CPLD is in the safe set: passives, the SDRAMs (they never
drive DQ without a clocked READ), the 060 only while CPU_RSTI reads low (060 in
reset: its outputs are high-Z) and the RAM FPGA only while the diag_pins image
(all inputs) is loaded. Plus the configured do-not-drive list.
"""

import os
import re
import socket
import subprocess
import tempfile
import time
from pathlib import Path

from . import netlist as NL
from . import protocol as P

# nets diag_pins.bin runs as clocks (PCLK, CLKCPU 25 MHz; PCLK_CPLD follows PCLK through the
# NB3N551) and BCLK: their CPLD pins sample at random, never a follower or a culprit
CLOCK_NETS = {"PCLK", "PCLK_CPLD", "CLKCPU", "BCLK"}
FLOAT_SAMPLES = 16
DO_NOT_DRIVE = {"CPU_RSTI", "PCLK", "BCLK", "CLKEN", "BRDMA", "PCLK_CPLD", "CPU_RSTO", "_CPURST"}
TAP = "xc.tap"
# leave EXTEST through BYPASS, then a TMS-high test-logic reset (jtag arp_init
# starts with a TLR); the CPLD pins go back to the programmed logic.
TLR_CMDS = [f"irscan {TAP} 0x{P.IR['BYPASS']:02X}", "jtag arp_init"]
LEN_MARKER = 0x3C5AA5C3
SUPPLY = re.compile(r"^(GND|VCC|VCC\d+|VCCIO.*|\+?\d+V\d*|V\d+V\d*)$", re.I)


class JtagError(RuntimeError):
    pass


def openocd_config(cfg):
    j = P.JTAG_PINS
    return "\n".join([
        "adapter driver linuxgpiod",
        f"adapter gpio tck {j['tck']} -chip {j['chip']}",
        f"adapter gpio tms {j['tms']} -chip {j['chip']}",
        f"adapter gpio tdi {j['tdi']} -chip {j['chip']}",
        f"adapter gpio tdo {j['tdo']} -chip {j['chip']}",
        "adapter speed 1000",
        "transport select jtag",
        "bindto 127.0.0.1",
        f"tcl_port {cfg.openocd_tcl_port}",
        "telnet_port disabled",
        "gdb_port disabled",
        f"jtag newtap xc tap -irlen {P.IRLEN} -ignore-version -expected-id 0x{P.IDCODE_VALUE:08x}",
        "",
    ])


class OpenocdLink:
    """openocd process + Tcl port. cmd() returns the command's result text;
    a Tcl error raises JtagError."""

    def __init__(self, cfg, events=None, start_timeout=10.0):
        self.cfg = cfg
        self.events = events
        fd, self.cfgfile = tempfile.mkstemp(prefix="tfdiag-openocd-", suffix=".cfg")
        with os.fdopen(fd, "w") as fh:
            fh.write(openocd_config(cfg))
        # openocd's log goes to a file: a pipe nobody drains would stall it
        self.logfh = tempfile.TemporaryFile(mode="w+", prefix="tfdiag-openocd-")
        self.proc = subprocess.Popen([cfg.openocd, "-f", self.cfgfile], stdout=subprocess.DEVNULL,
                                     stderr=self.logfh)
        self.sock = None
        deadline = time.monotonic() + start_timeout
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                self.logfh.seek(0)
                err = self.logfh.read()[-400:]
                self._cleanup()
                raise JtagError(f"openocd exited (status {self.proc.returncode}): {err.strip()}")
            try:
                self.sock = socket.create_connection(("127.0.0.1", cfg.openocd_tcl_port), timeout=2)
                break
            except OSError:
                time.sleep(0.1)
        if self.sock is None:
            self.close()
            raise JtagError("openocd Tcl port did not open")
        self.sock.settimeout(30)

    def raw(self, text):
        self.sock.sendall(text.encode() + b"\x1a")
        buf = b""
        while not buf.endswith(b"\x1a"):
            chunk = self.sock.recv(65536)
            if not chunk:
                raise JtagError("openocd closed the Tcl connection")
            buf += chunk
        return buf[:-1].decode(errors="replace")

    def cmd(self, text):
        r = self.raw("set _rc [catch {%s} _r]; set _o \"$_rc $_r\"" % text)
        rc, _, out = r.partition(" ")
        if rc != "0":
            raise JtagError(f"{text.split()[0]}: {out.strip()}")
        return out.strip()

    def close(self):
        if self.sock is not None:
            try:
                self.raw("shutdown")
            except Exception:
                pass
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None
        if self.proc.poll() is None:
            try:
                self.proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()
        self._cleanup()

    def _cleanup(self):
        try:
            self.logfh.close()
        except Exception:
            pass
        try:
            os.unlink(self.cfgfile)
        except OSError:
            pass


def vec_fields(v, nfields=P.BSR_FIELDS):
    return " ".join(f"32 0x{(v >> (32 * k)) & 0xFFFFFFFF:08x}" for k in range(nfields))


def parse_fields(text):
    v = 0
    for k, h in enumerate(text.split()):
        v |= int(h, 16) << (32 * k)
    return v


class Cpld:
    def __init__(self, link, netlist=None, events=None):
        self.link = link
        self.nl = netlist or NL.load()
        self.events = events
        self.pins = self.nl.cpld_pins

    def ir(self, name):
        if name not in P.IR:
            raise JtagError(f"refusing IR {name}")
        self.link.cmd(f"irscan {TAP} 0x{P.IR[name]:02X}")

    def dr(self, v, nfields=P.BSR_FIELDS):
        return parse_fields(self.link.cmd(f"drscan {TAP} {vec_fields(v, nfields)}"))

    def init(self):
        try:
            self.link.cmd("init")
        except JtagError as e:
            # openocd complains when the IDCODE is unexpected but the chain still works
            if self.events:
                self.events.warn(f"openocd init: {e}")

    def idcode(self):
        self.ir("IDCODE")
        return self.dr(0, 1)

    def usercode(self):
        self.ir("USERCODE")
        return self.dr(0, 1)

    def bypass_ok(self):
        self.ir("BYPASS")
        pat = 0x5A5AC3C3
        out = self.dr(pat, 1)
        return out == ((pat << 1) & 0xFFFFFFFF)

    def boundary_length(self):
        """Shift a marker through SAMPLE/PRELOAD; return the register length (or None).
        The bits left in the register are zeros (all outputs disabled if EXTEST follows)."""
        self.ir("SAMPLE")
        total = P.BSR_FIELDS + 1
        out = self.dr(LEN_MARKER, total)
        for length in [P.BSR_LEN] + list(range(1, 32 * total - 31)):
            if (out >> length) & 0xFFFFFFFF == LEN_MARKER:
                return length
        return None

    def chain_check(self):
        """IDCODE, USERCODE, BYPASS and boundary length."""
        self.init()
        res = {}
        try:
            idc = self.idcode()
        except JtagError as e:
            return {"ok": False, "error": str(e)}
        res["idcode"] = f"0x{idc:08x}"
        res["idcode_ok"] = (idc & P.IDCODE_MASK) == P.IDCODE_VALUE
        if not res["idcode_ok"]:
            res["ok"] = False
            res["error"] = ("no CPLD on the JTAG chain" if idc in (0, 0xFFFFFFFF)
                            else f"unexpected IDCODE 0x{idc:08x}")
            return res
        res["usercode"] = f"0x{self.usercode():08x}"
        res["bypass_ok"] = self.bypass_ok()
        res["boundary_length"] = self.boundary_length()
        res["ok"] = res["bypass_ok"] and res["boundary_length"] == P.BSR_LEN
        return res

    def ids(self):
        """(idcode, usercode) as hex strings, or (None, None) if the chain is dead."""
        self.init()
        try:
            idc = self.idcode()
            if (idc & P.IDCODE_MASK) != P.IDCODE_VALUE:
                return (f"0x{idc:08x}" if idc not in (0, 0xFFFFFFFF) else None), None
            return f"0x{idc:08x}", f"0x{self.usercode():08x}"
        except JtagError:
            return None, None
        finally:
            self.tlr()

    def tlr(self):
        for c in TLR_CMDS:
            try:
                self.link.cmd(c)
            except JtagError as e:
                if self.events:
                    self.events.warn(f"TLR: {c}: {e}")

    # ---- SAMPLE --------------------------------------------------------------
    def sample(self, n=16, interval=0.01):
        self.ir("SAMPLE")
        rows = []
        for _ in range(n):
            rows.append(self.dr(0))
            if interval:
                time.sleep(interval)
        return rows

    def decode(self, rows):
        """Per named CPLD pin: level summary and whether the CPLD drives it."""
        out = {}
        N = len(rows)
        for pin, e in self.pins.items():
            name = e.get("ucf")
            if not name or "in" not in e:
                continue
            hi = sum((v >> e["in"]) & 1 for v in rows)
            drv = sum((v >> e["ctl"]) & 1 for v in rows) if "ctl" in e else 0
            state = "high" if hi == N else "low" if hi == 0 else "toggling"
            out[name] = {"pin": int(pin), "net": e["net"], "state": state, "high": hi, "samples": N,
                         "cpld_drives": drv == N if drv in (0, N) else f"{drv}/{N}"}
        return out

    def level(self, v, name):
        pin = self.nl.cpld_pin_by_name(name)
        e = self.pins.get(pin) if pin else None
        if not e or "in" not in e:
            return None
        return (v >> e["in"]) & 1

    # ---- EXTEST interconnect -----------------------------------------------------
    def drivable(self, cpu_in_reset, fpga_inputs, extra_dnd=(), sdram_parked=False):
        """[(pin, name, net)] the test may drive, and {name: reason} for those it won't.

        cpu_in_reset: CPU_RSTI reads low (the 060 three-states its bus in reset).
        fpga_inputs: the pin readback image answers (all FPGA pins inputs except
        protocol.PINREAD_DRIVEN). sdram_parked: that image parks the SDRAMs (CS# high,
        CKE low, DQM high; diag ID feature bit 7): only then may a net shared with the
        SDRAM DQ/address pins be driven."""
        dnd = DO_NOT_DRIVE | set(extra_dnd)
        ok, skipped = [], {}
        for pin, e in sorted(self.pins.items(), key=lambda x: int(x[0])):
            name, net = e.get("ucf"), e.get("net")
            if not name or "out" not in e or "ctl" not in e or "in" not in e:
                continue
            if name in dnd or net in dnd:
                skipped[name] = "do-not-drive list"
                continue
            why = None
            for part, pad in self.nl.nets.get(net, {}).get("members", []):
                if part == self.nl.cpld_part or self.nl.passive(part):
                    continue
                if part in NL.SDRAM:
                    if not sdram_parked:
                        why = "SDRAM not parked"
                    continue
                if part == NL.CPU:
                    if not cpu_in_reset:
                        why = "060 not held in reset"
                    continue
                if part == self.nl.fpga_part:
                    if not fpga_inputs:
                        why = "RAM FPGA image may drive it"
                    elif str(pad) in P.PINREAD_DRIVEN:
                        why = f"driven by the pin readback image (FPGA pin {pad})"
                    continue
                why = f"also driven by {part}"
                break
            if why:
                skipped[name] = why
            else:
                ok.append((pin, name, net))
        return ok, skipped

    def vector(self, pin=None, val=0):
        v = 0
        if pin is not None:
            e = self.pins[pin]
            v |= 1 << e["ctl"]
            if val:
                v |= 1 << e["out"]
        return v

    def extest(self, targets, fpga_read=None, progress=None, float_samples=FLOAT_SAMPLES):
        """Walk 0 and 1 over each target (all other CPLD pins high-Z).

        Returns raw steps: [{"name", "pin", "val", "cpld": int, "fpga": {pin: level}|None}]:
        first `float_samples` all-high-Z captures ("float"; pins that change across them are
        toggling or floating), then the walk, then a float re-check ("float_end")."""
        z = self.vector()
        steps = []
        self.ir("SAMPLE")
        self.dr(z)                       # preload: every output disabled
        try:
            self.ir("EXTEST")
            self.dr(z)

            def take(name, pin, val, v):
                self.dr(v)
                cap = self.dr(v)         # the second scan captures with v applied
                steps.append({"name": name, "pin": pin, "val": val, "cpld": cap,
                              "fpga": fpga_read() if fpga_read else None})

            for _ in range(max(1, float_samples)):
                take("float", None, None, z)
            for i, (pin, name, net) in enumerate(targets):
                for val in (0, 1):
                    take(name, pin, val, self.vector(pin, val))
                if progress:
                    progress(i + 1, len(targets), name)
            take("float_end", None, None, z)
        finally:
            try:
                self.dr(z)
            finally:
                self.tlr()
        return steps

    def clock_net(self, net, ucf=None):
        if net in CLOCK_NETS or ucf in CLOCK_NETS:
            return True
        return bool(set(self.nl.nets.get(net, {}).get("fpga_pins", [])) & P.PINREAD_DRIVEN)

    def unstable(self, steps):
        """CPLD pins (UCF names) and FPGA pins whose level is not steady with nothing driven:
        they change across the float captures or between them and the float re-check
        (toggling clocks, floating lines), plus the clock nets. They are never followers."""
        named = [(p, e) for p, e in self.pins.items() if e.get("ucf") and "in" in e]
        g = lambda v, e: (v >> e["in"]) & 1
        floats = [s for s in steps if s["name"] in ("float", "float_end")]
        cpld = {e["ucf"] for _, e in named if len({g(s["cpld"], e) for s in floats}) > 1}
        clocks = {e["ucf"] for _, e in named if self.clock_net(e["net"], e["ucf"])}
        fpga = set()
        fl = [s["fpga"] for s in floats if s.get("fpga") is not None]
        if fl:
            for fp in fl[0]:
                if len({f.get(fp) for f in fl}) > 1:
                    fpga.add(fp)
        fpga |= set(P.PINREAD_DRIVEN)
        return {"cpld": cpld, "clocks": clocks, "fpga": fpga}

    def analyse(self, steps, targets):
        """Per driven net: ok / stuck / short (with neighbour hints) / FPGA open.
        Toggling or floating pins and clock nets (see unstable()) never count as followers;
        a floating net that is driven is still tested (its followers must be steady)."""
        named = [(p, e) for p, e in self.pins.items() if e.get("ucf") and "in" in e]
        g = lambda v, e: (v >> e["in"]) & 1
        base = steps[0]
        end = steps[-1]
        un = self.unstable(steps)
        skip = un["cpld"] | un["clocks"]
        by = {}
        for s in steps:
            if s["name"] not in ("float", "float_end"):
                by.setdefault(s["pin"], {})[s["val"]] = s
        findings = []
        nets = []
        for pin, name, net in targets:
            d = by.get(pin, {})
            if 0 not in d or 1 not in d:
                continue
            e = self.pins[pin]
            own = (g(d[0]["cpld"], e), g(d[1]["cpld"], e))
            rec = {"name": name, "net": net, "cpld_pin": int(pin), "status": "info", "msg": ""}
            msgs = []
            if own != (0, 1):
                rec["status"] = "finding"
                held = "low" if own == (0, 0) else "high" if own == (1, 1) else "inverted"
                sup = [(n, self.nl.cpld_pins.get(n, {}).get("net")) for n in self.nl.neighbours(self.nl.cpld_part, pin)]
                sup = [(n, sn) for n, sn in sup if sn and SUPPLY.match(sn)]
                hint = "".join(f"; neighbour pin {n} is {sn}: likely a solder bridge" for n, sn in sup)
                msgs.append(f"CPLD pin {pin} reads {own[0]}/{own[1]} when driven 0/1: held {held} "
                            "(short to a supply or a driver, or a damaged CPLD output)" + hint)
                findings.append({"kind": "stuck", "a": name, "level": held, "supply_neighbours": sup})
            follow = [(p2, e2) for p2, e2 in named if p2 != pin and e2["net"] != net
                      and e2["ucf"] not in skip
                      and g(d[0]["cpld"], e2) == 0 and g(d[1]["cpld"], e2) == 1]
            for p2, e2 in follow:
                adj = self.nl.adjacent(net, e2["net"])
                hint = ("; adjacent pins " + ", ".join(f"{pt} {a}/{b}" for pt, a, b in adj) +
                        ": likely a solder bridge") if adj else ""
                rec["status"] = "finding"
                msgs.append(f"shorted to {e2['ucf']} (CPLD pin {p2}){hint}")
                findings.append({"kind": "short", "a": name, "b": e2["ucf"], "adjacent": adj})
            f0, f1 = d[0].get("fpga"), d[1].get("fpga")
            if f0 is not None and f1 is not None:
                mine = self.nl.nets.get(net, {}).get("fpga_pins", [])
                for fp in mine:
                    if fp not in f0 or fp in P.PINREAD_DRIVEN:
                        continue
                    if (f0.get(fp), f1.get(fp)) != (0, 1):
                        rec["status"] = "finding"
                        msgs.append(f"FPGA pin {fp} reads {f0.get(fp)}/{f1.get(fp)}: open between "
                                    f"CPLD pin {pin} and FPGA pin {fp}")
                        findings.append({"kind": "open", "a": name, "fpga_pin": fp})
                for fp, lv0 in f0.items():
                    if fp in mine or fp not in self.nl.fpga_pins or fp in un["fpga"]:
                        continue
                    if lv0 == 0 and f1.get(fp) == 1:
                        other = self.nl.fpga_pins[fp]["net"]
                        if any(e2["net"] == other for _, e2 in follow):
                            continue
                        adj = self.nl.adjacent(net, other)
                        hint = ("; adjacent pins " + ", ".join(f"{pt} {a}/{b}" for pt, a, b in adj)
                                + ": likely a solder bridge") if adj else ""
                        rec["status"] = "finding"
                        msgs.append(f"FPGA pin {fp} ({other}) follows it{hint}")
                        findings.append({"kind": "short", "a": name, "b": other, "adjacent": adj})
            rec["msg"] = "; ".join(msgs) or "drives 0 and 1, no other pin follows"
            nets.append(rec)
        float_levels = {e["ucf"]: g(base["cpld"], e) for _, e in named}
        # clock nets (PCLK runs on purpose in the pin readback image) always read differently
        drift = [e["ucf"] for _, e in named if g(end["cpld"], e) != g(base["cpld"], e)
                 and e["ucf"] not in un["clocks"]]
        order = {e["ucf"]: int(p) for p, e in named}
        return {"nets": nets, "findings": findings, "float": float_levels, "float_changed": drift,
                "unstable": sorted(un["cpld"] - un["clocks"], key=lambda n: order.get(n, 0)),
                "clocks": sorted(un["clocks"], key=lambda n: order.get(n, 0)),
                "unstable_fpga_pins": sorted(un["fpga"] - set(P.PINREAD_DRIVEN), key=int)}
