from tfdiag.tests import helpers  # noqa: F401
from tfdiag.tests.fakes import FakeSpiDev
from tfdiag import amiga


class Cycles(helpers.Base):
    def test_read_write_sterm_stale(self):
        cyc = FakeSpiDev._cycle
        ents = [(0xFFF, 10)] + cyc(20) + cyc(30, write=True) + cyc(6, sterm=True) + cyc(20, stale=True)
        c = amiga.cycles(ents, fabric_mhz=100.0)
        self.assertEqual(len(c), 4)
        r, w, st, sd = c
        self.assertEqual((r["rw"], r["as_to_dsack"], r["ds_to_dsack"], r["as_width"]), ("read", 200.0, 180.0, 240.0))
        self.assertEqual(r["dsack_release"], 20.0)
        self.assertEqual((w["rw"], w["as_to_dsack"]), ("write", 300.0))
        self.assertEqual(st["as_to_sterm"], 60.0)
        self.assertNotIn("as_to_dsack", st)
        self.assertFalse(r["stale_dsack"])
        self.assertTrue(sd["stale_dsack"])

    def test_incomplete_cycle_dropped(self):
        ents = [(0xFFF, 10), (0xFFE, 5)]
        self.assertEqual(amiga.cycles(ents), [])



class Histograms(helpers.Base):
    def test_summary(self):
        from tfdiag import protocol as P
        bins = [0] * 32
        bins[3], bins[5], bins[31] = 10, 20, 1
        sm = amiga.hist_summary(0, bins, 1, 100.0)        # 2-clk bins at 100 MHz
        self.assertEqual((sm["n"], sm["min"], sm["median"], sm["max"], sm["open"]), (31, 60.0, 100.0, 620.0, True))
        self.assertNotIn("bin0", sm)
        self.assertNotIn("saturated", sm)
        bins[0] = 7
        sm = amiga.hist_summary(7, bins, 0, 50.0)         # stale DSACK: bin 0 = not stale
        self.assertEqual((sm["bin0"], sm["n"], sm["min"]), (7, 31, 60.0))
        self.assertEqual(len(P.HIST_MEAS), P.HIST_MEASUREMENTS)
