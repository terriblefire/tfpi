import socket
import unittest

from tfdiag.tests import helpers
from tfdiag.tests import fakes
from tfdiag import jtag as J, spi as S


class Jtag(helpers.Base):
    def cpld(self):
        return J.Cpld(fakes.FakeLink(self.env.state_path))

    def test_chain_check(self):
        r = self.cpld().chain_check()
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["idcode"], "0x59616093")
        self.assertEqual(r["usercode"], f"0x{fakes.USERCODE:08x}")
        self.assertEqual(r["boundary_length"], 864)

    def test_no_chain(self):
        self.env.board.update(lambda s: s.__setitem__("powered", False))
        r = self.cpld().chain_check()
        self.assertFalse(r["ok"])
        self.assertIn("no CPLD", r["error"])
        self.assertEqual(self.cpld().ids(), (None, None))

    def test_sample_decode(self):
        self.env.board.update(lambda s: s.__setitem__("stuck", {"TS": 0}))
        c = self.cpld()
        d = c.decode(c.sample(3, interval=0))
        self.assertEqual(d["TS"]["state"], "low")
        self.assertEqual(d["AS20"]["state"], "high")

    def _extest(self, cpu_reset=True, fpga=True):
        if cpu_reset:
            self.env.board.update(lambda s: s.__setitem__("cpu_rsti", 0))
        if fpga:
            self.env.board.update(lambda s: s.__setitem__("image", "diag_pins"))
        c = self.cpld()
        spi = S.Spi(fakes.FakeSpiDev(self.env.state_path))
        targets, skipped = c.drivable(cpu_reset, fpga)
        steps = c.extest(targets, spi.pinread if fpga else None)
        return c, targets, skipped, c.analyse(steps, targets)

    def test_extest_clean(self):
        c, targets, skipped, an = self._extest()
        names = {t[1] for t in targets}
        self.assertIn("TS", names)
        for n in ("CPU_RSTI", "PCLK", "BCLK", "CLKEN", "BRDMA"):
            self.assertNotIn(n, names)
        self.assertEqual(skipped["CPU_RSTI"], "do-not-drive list")
        self.assertTrue(any("also driven by" in v for v in skipped.values()))
        bad = [n for n in an["nets"] if n["status"] != "info"]
        self.assertEqual(bad, [])
        s = self.board()
        self.assertEqual(s["jtag"]["ir"], "IDCODE")        # ended with a TLR
        self.assertFalse(s["jtag"]["extest"])
        self.assertEqual(s["forbidden"], [])

    def test_extest_short_between_neighbours(self):
        nb = self.env.board  # TS is CPLD pin 54; pin 55's net is its neighbour
        from tfdiag import netlist
        nl = netlist.load()
        other = nl.cpld_pins["53"]["net"]
        nb.update(lambda s: s.__setitem__("shorts", [["TS", other]]))
        c, targets, skipped, an = self._extest()
        ts = next(n for n in an["nets"] if n["name"] == "TS")
        self.assertEqual(ts["status"], "finding")
        self.assertIn("likely a solder bridge", ts["msg"])
        self.assertIn("BUSCPLD 54/53", ts["msg"])

    def test_extest_stuck_and_open(self):
        self.env.board.update(lambda s: (s.__setitem__("stuck", {"TIP": 0}), s.__setitem__("opens", ["44"])))
        c, targets, skipped, an = self._extest()
        by = {n["name"]: n for n in an["nets"]}
        self.assertIn("held low", by["TIP"]["msg"])
        self.assertIn("open between CPLD pin 54 and FPGA pin 44", by["TS"]["msg"])

    def test_stuck_high_next_to_supply(self):
        self.env.board.update(lambda s: s.__setitem__("stuck", {"TS": 1}))
        c, targets, skipped, an = self._extest()
        ts = next(n for n in an["nets"] if n["name"] == "TS")
        self.assertIn("neighbour pin 55 is VCC33", ts["msg"])

    def test_toggling_clock_and_floating_lines_are_not_findings(self):
        """Modelled on the T board test (t_board2): diag_pins runs PCLK at 25 MHz, so CPLD pin
        38 (PCLK_CPLD) samples at random, and the Amiga-side D16-D23 float in reset and drift.
        None of that may become a short; a real bridge must still be found."""
        drift = {f"D{i}": 60 for i in range(16, 24)}
        self.env.board.update(lambda s: (s.__setitem__("toggle", ["PCLK_CPLD"]), s.__setitem__("drift", drift),
                                         s.__setitem__("shorts", [["TS", "SIZ60_0"]])))
        c, targets, skipped, an = self._extest()
        msgs = " ".join(n["msg"] for n in an["nets"])
        self.assertNotIn("PCLK", msgs)
        self.assertNotIn("D<16>", msgs)
        self.assertIn("PCLK", an["clocks"])
        for i in range(16, 24):
            self.assertIn(f"D<{i}>", an["unstable"])
        bad = {n["name"] for n in an["nets"] if n["status"] == "finding"}
        self.assertEqual(bad, {"TS", "SIZ60<0>"})                    # the real short only
        ts = next(n for n in an["nets"] if n["name"] == "TS")
        self.assertIn("shorted to SIZ60<0>", ts["msg"])
        self.assertNotIn("PCLK", an["float_changed"])            # a running clock is not "floating"
        from tfdiag import boardview
        fs = boardview.findings({"board": {"extest": dict(an, float_changed=["D<16>", "D<17>"])}})
        fl = [f for f in fs if f["kind"] == "float"]
        self.assertEqual(len(fl), 1)
        self.assertIn("Amiga CPU-slot lines", fl[0]["detail"])
        self.assertIn("can mean IOR is bad", fl[0]["detail"])

    def test_float_captures(self):
        self.env.board.update(lambda s: s.__setitem__("toggle", ["PCLK_CPLD"]))
        c = self.cpld()
        targets, _ = c.drivable(True, False, sdram_parked=True)
        steps = c.extest(targets[:2])
        self.assertEqual([s["name"] for s in steps[:16]], ["float"] * 16)
        self.assertEqual(steps[-1]["name"], "float_end")
        self.assertIn("PCLK", c.unstable(steps)["cpld"] | c.unstable(steps)["clocks"])

    def test_cpu_not_in_reset_skips_060_nets(self):
        c, targets, skipped, an = self._extest(cpu_reset=False)
        self.assertNotIn("TS", {t[1] for t in targets})
        self.assertEqual(skipped["TS"], "060 not held in reset")

    def test_sdram_nets_need_parked_sdram(self):
        from tfdiag import netlist
        nl = netlist.load()
        sd = sorted({e["ucf"] for e in nl.cpld_pins.values() if e.get("ucf")
                     and any(p in netlist.SDRAM for p, _ in nl.nets[e["net"]]["members"])})
        self.assertTrue(sd)                                   # the 060 data/address lines
        c = self.cpld()
        targets, skipped = c.drivable(True, True)             # default: not parked
        names = {t[1] for t in targets}
        for n in sd:
            self.assertNotIn(n, names)
            self.assertIn(skipped[n], ("SDRAM not parked", "do-not-drive list")
                          + tuple(v for v in skipped.values() if v.startswith("also driven")))
        self.assertTrue(any(v == "SDRAM not parked" for v in skipped.values()))
        targets, skipped = c.drivable(True, True, sdram_parked=True)
        self.assertTrue(set(sd) & {t[1] for t in targets})
        self.assertFalse(any(v == "SDRAM not parked" for v in skipped.values()))

    def test_board_reads_parked_bit(self):
        from tfdiag import board, events as E, fpga as F
        for feat, parked in ((0x90, True), (0x10, False)):
            self.env.board.update(lambda s: s.__setitem__("pins_features", feat))
            spi = S.Spi(fakes.FakeSpiDev(self.env.state_path))
            res = board.run_board(self.cpld(), spi, F.Fpga(self.env.cfg, E.Quiet()), E.Quiet(),
                                  self.env.tmp / f"b{feat}", self.env.images, helpers.T_INFO(self.env))
            self.assertEqual(res["sdram_parked"], parked)
            self.assertEqual("SDRAM not parked" in res["skipped"].values(), not parked)

    def test_parked_ide_pins_read_back(self):
        from tfdiag import board, events as E, fpga as F
        for stuck, want in (({}, []), ({"IOR": 0}, ["parked_IOR"])):
            self.env.board.update(lambda s: s.__setitem__("stuck", stuck))
            ev = E.Quiet()
            spi = S.Spi(fakes.FakeSpiDev(self.env.state_path))
            res = board.run_board(self.cpld(), spi, F.Fpga(self.env.cfg, E.Quiet()), ev,
                                  self.env.tmp / f"p{len(want)}", self.env.images, helpers.T_INFO(self.env))
            got = [r["name"] for r in ev.results if r["name"].startswith("parked_") and r["status"] == "finding"]
            self.assertEqual(got, want)
            self.assertEqual(res["parked_readback"]["IOR"], 0 if want else 1)
            if want:
                msg = next(r["msg"] for r in ev.results if r["name"] == "parked_IOR")
                self.assertIn("IC4/IC5", msg)

    def test_passive_rules(self):
        from tfdiag import netlist
        nl = netlist.load()
        self.assertEqual(nl.ohms("R12"), 22)                  # 22R series resistor
        self.assertEqual(nl.ohms("RN1"), 2200)                # CAY16-222J4LF
        self.assertEqual(nl.ohms("R8"), 1000)
        self.assertFalse(nl.passive("R12"))                   # links PCLK_CPLD to the clock buffer
        self.assertTrue(nl.passive("RN2"))
        self.assertFalse(nl.passive("JP1"))                   # a jumper can tie a net to a supply
        self.assertFalse(nl.passive("MMUDIS1"))
        self.assertTrue(nl.passive("C16"))

    def test_driven_pin_image_pins_skipped(self):
        from tfdiag import netlist, protocol as P
        nl = netlist.load()
        c = self.cpld()
        targets, skipped = c.drivable(True, True, extra_dnd=(), sdram_parked=True)
        for pin, e in nl.cpld_pins.items():
            if e.get("ucf") and set(nl.nets[e["net"]]["fpga_pins"]) & P.PINREAD_DRIVEN:
                self.assertNotIn(e["ucf"], {t[1] for t in targets})

    def test_normal_fpga_image_skips_fpga_nets(self):
        c = self.cpld()
        targets, skipped = c.drivable(True, False)
        self.assertEqual(skipped["TS"], "RAM FPGA image may drive it")


class RealLink(helpers.Base):
    """jtag.OpenocdLink against the fake openocd Tcl server."""

    def test_openocd_link(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        self.env.cfg.openocd_tcl_port = port
        link = J.OpenocdLink(self.env.cfg)
        try:
            c = J.Cpld(link)
            r = c.chain_check()
            self.assertTrue(r["ok"], r)
            with self.assertRaises(J.JtagError):
                link.cmd("bogus")
            c.tlr()
        finally:
            link.close()
        self.assertEqual(self.board()["jtag"]["ir"], "IDCODE")


if __name__ == "__main__":
    unittest.main()
