import xml.etree.ElementTree as ET

from tfdiag.tests import helpers
from tfdiag.tests.fakes import FakeSpiDev
from tfdiag import vcd, vcdsvg


class VcdSvg(helpers.Base):
    def capture(self, name="wave_p0_k3_r2.vcd"):
        cyc = FakeSpiDev._cycle
        ents = [(0xFFF, 10)] * 2 + cyc(20) + cyc(140) + cyc(30, write=True) + cyc(6, sterm=True)
        path = self.env.tmp / name
        vcd.write(path, ents, trigger_index=2 + 6, fabric_mhz=100.0,
                  title="preset 0, trigger AS20 fall in region, region cia")
        return path

    def test_render(self):
        path = self.capture()
        meta = vcdsvg.convert(path, all_signals=False, show_all=True)
        svg = (self.env.tmp / "wave_p0_k3_r2.svg").read_text()
        ET.fromstring(svg)                                      # well-formed XML
        self.assertEqual(meta["cycles"], 4)
        self.assertEqual(meta["region"], "cia")
        self.assertAlmostEqual(meta["trigger_ns"], 560.0)       # 2 x 100 ns idle + the first cycle with its idle (360 ns)
        self.assertIn("cia R DS→DSACK 1380 ns", svg)       # the trigger cycle carries the region
        self.assertIn("R DS→DSACK 180 ns", svg)
        self.assertIn("W DS→DSACK 280 ns", svg)
        self.assertIn("STERM 60 ns", svg)
        self.assertIn(">trigger<", svg)
        self.assertIn(">AS20<", svg)
        self.assertNotIn(">BGACK<", svg)                        # constant signals hidden by default
        self.assertIn("prefers-color-scheme", svg)

    def test_address_log_labels(self):
        cyc = FakeSpiDev._cycle
        ents = [(0xFFF, 10)] * 2 + cyc(20, write=True) + cyc(36)
        events = [(2, {"addr": 0x00012340, "siz": 0, "rw": 0, "region": 1}),
                  (8, {"addr": 0x00BFE001, "siz": 1, "rw": 1, "region": 2})]
        path = self.env.tmp / "wave_p0_k3_r2.vcd"
        vcd.write(path, ents, trigger_index=8, fabric_mhz=100.0, events=events,
                  title="preset 0, trigger AS20 fall in region, region cia")
        text = path.read_text()
        self.assertIn("$var wire 32 ", text)
        self.assertIn("b00000000000000010010001101000000 ", text)
        meta = vcdsvg.convert(path, show_all=True)
        svg = (self.env.tmp / "wave_p0_k3_r2.svg").read_text()
        ET.fromstring(svg)
        self.assertTrue(meta["address_log"])
        self.assertIn("W.L $00012340 chip, DS\u2192DSACK 180 ns", svg)
        self.assertIn("R.B $00BFE001 cia, DS\u2192DSACK 340 ns", svg)
        self.assertIn(">ADDR<", svg)
        self.assertNotIn(">REGION<", svg)                     # vectors are labels, not rows

    def test_fit_label(self):
        lab = "W.L $00012340 chip, DS\u2192DSACK 180 ns"
        self.assertEqual(vcdsvg.fit_label(lab, 400), lab)
        self.assertEqual(vcdsvg.fit_label(lab, 140), "W.L $00012340 chip")
        self.assertEqual(vcdsvg.fit_label(lab, 100), "W.L $00012340")
        self.assertEqual(vcdsvg.fit_label(lab, 40), "W.L")
        self.assertIsNone(vcdsvg.fit_label(lab, 10))

    def test_window_and_cli(self):
        path = self.capture("wave_p0_k4_r0.vcd")
        rc, evs = helpers.run_cli(self.env, ["vcd-svg", str(path), "--start-ns", "400", "--span-ns", "500",
                                             "--all-signals", "--out", str(self.env.tmp / "x.svg")])
        self.assertEqual(rc, 0)
        self.assertEqual(evs[-1]["files"], [str(self.env.tmp / "x.svg")])
        svg = (self.env.tmp / "x.svg").read_text()
        self.assertIn(">BGACK<", svg)
        self.assertIn(">400<", svg)                             # time axis in ns
        self.assertEqual(vcdsvg.region_of(vcdsvg.parse_vcd(path.read_text().replace("region cia", "region any")),
                                          path), "ramsey")      # STERM capture
        rc, evs = helpers.run_cli(self.env, ["vcd-png", str(path)])   # alias
        self.assertEqual(rc, 0)
