from tfdiag.tests import helpers
from tfdiag.tests import fakes
from tfdiag import spi as S, protocol as P, timing as T


class Spi(helpers.Base):
    state = {"image": "diag_timing"}

    def spi(self):
        return S.Spi(fakes.FakeSpiDev(self.env.state_path, self.env.clock))

    def test_sram_roundtrip_chunked(self):
        s = self.spi()
        data = bytes(range(256)) * 20            # crosses SPI_CHUNK
        s.sram_write(0x0100, data)
        self.assertEqual(s.sram_read(0x0100, len(data)), data)
        self.assertEqual(s.proto_version(), 2)

    def test_diag_id(self):
        d = self.spi().diag_id()
        self.assertEqual(d["commit"], "0abc1234")
        self.assertEqual(d["features"], ["waveform", "clock_counters", "reset", "histograms", "cycle_counters"])
        self.env.board.update(lambda s: s.__setitem__("features", 0x6F))
        self.assertIn("tprobe", self.spi().diag_id()["features"])

    def test_no_diag_on_normal_image(self):
        self.env.board.update(lambda s: s.__setitem__("image", "flash"))
        self.assertIsNone(self.spi().diag_id())

    def test_tprobe(self):
        s = self.spi()
        s.tprobe_select(3, P.REGIONS["chip"])
        h = s.tprobe_read()
        self.assertEqual((h["sel"], h["region"], h["bins"][5]), (3, 1, 100))

    def test_wave(self):
        s = self.spi()
        s.wave_arm(1, 0)
        st = s.wave_status()
        self.assertTrue(st["triggered"])
        e = s.wave_entries(st)
        self.assertEqual(len(e), P.WAVE_ENTRIES)
        self.assertEqual(e[127], (0xFFF, 10))                 # idle before the trigger
        self.assertEqual(e[128][0] & 1, 0)                    # AS20 low at the trigger entry
        self.assertEqual(st["trigger"], 128)

    def test_wave_addr_layout(self):
        self.assertEqual(P.wave_addr(0, 0), 0x2000)
        self.assertEqual(P.wave_addr(5, 1), 0x2205)
        self.assertEqual(P.wave_addr(511, 2), 0x25FF)        # the last byte below the histograms
        self.assertLess(P.wave_addr(511, 2), P.HIST_LO)

    def test_histograms(self):
        s = self.spi()
        s.hist_start(1, P.HIST_REGIONS["chip"])
        self.env.clock.sleep(0.01)                  # 1M cycles/s in the fake: 10000 in 10 ms
        st = s.hist_status()
        self.assertEqual((st["shift"], st["region"], st["clearing"], st["lost"]), (1, 1, False, 2))
        self.assertIn(st["as"], (9999, 10000))
        bins = s.hist_bins()
        self.assertEqual(len(bins), 8)
        self.assertIn(bins[0][10], (4999, 5000))    # DS20 -> DSACK reads: 20 clks >> 1
        self.assertIn(bins[3][12], (6999, 7000))    # counts > 255 use the high-byte block
        self.env.clock.sleep(1.0)
        self.assertEqual(s.hist_bins()[3][12], 0xFFFF)   # 16-bit counters saturate
        s.hist_start(0, P.HIST_REGIONS["ramsey"])
        self.env.clock.sleep(0.01)
        self.assertIn(s.hist_bins()[2][6], (899, 900))   # STERM after 6 clks

    def test_pinread(self):
        self.env.board.update(lambda s: (s.__setitem__("image", "diag_pins"),
                                          s.__setitem__("stuck", {"TS": 0})))
        pins = self.spi().pinread()
        self.assertEqual(pins["44"], 0)        # TS
        self.assertEqual(pins["138"], 1)       # AS20
        self.assertEqual(len(pins), len(P.PINREAD_ORDER))

    def test_body_checks(self):
        self.assertEqual(P.body_sum(bytes.fromhex("00000001 00000002 80000000")), 0x80000008)
        body = helpers.make_body(b"\x01\x02\x03\x04" * 4)
        self.assertEqual(T.check_body(body)["length"], 16)
        for bad in (body[:-1], b"XXXX" + body[4:], body[:-4] + b"\0\0\0\1",
                    helpers.make_body(b"\0" * P.BODY_MAX)):
            with self.assertRaises(T.TimingError):
                T.check_body(bad)

    def test_plan_bytes(self):
        b = T.plan_bytes([0, 2, 5], 0x1FF, start=1, iters=4, append=True)
        self.assertEqual(len(b), P.MB_PLAN_END - P.MB_GO)
        self.assertEqual(b[:4], b"TFGO")
        self.assertEqual(b[P.MB_NPRE - P.MB_GO:P.MB_NPRE - P.MB_GO + 2], b"\x00\x03")
        self.assertEqual(b[P.MB_PRE - P.MB_GO:P.MB_PRE - P.MB_GO + 3], bytes([0, 2, 5]))
        self.assertEqual(b[P.MB_START - P.MB_GO:], bytes.fromhex("000100040001"))

    def test_clock_frame(self):
        c = self.spi().clocks_latch()
        self.assertEqual(c["gate"], 1 << 22)
        self.assertEqual(c["CLKAUX"], (1 << 22) // 4)
        self.assertEqual(c["seq"], 7)

    def test_cycle_counters(self):
        s = self.spi()
        s.cycle_clear(3)
        c = s.cycle_counters()
        self.assertEqual((c["as"], c["sterm"], c["dsack"], c["fast"], c["threshold"]), (1000, 300, 700, 650, 3))
        self.assertEqual((c["sterm_min"], c["sterm_max"], c["dsack_ta_min"], c["dsack_ta_max"]), (5, 7, 2, 4))

    def test_address_log(self):
        s = self.spi()
        dev = s.dev
        struct = __import__("struct")
        struct.pack_into(">I", dev.sram, P.MAILBOX + P.MB_STATUS, P.STATUS_BODY_RUNNING)
        s.wave_arm(3, P.HIST_REGIONS["cia"], log=True)
        ws = s.wave_status()                                  # triggers the fake capture
        ls = s.log_status()
        self.assertTrue(ls["frozen"] and ls["trigger_seen"])
        log = s.log_entries(ls)
        self.assertEqual(len(log), ls["count"])
        t = log[ls["trigger_entry"]]
        self.assertEqual((t["region"], t["siz"], t["rw"], t["addr"] >> 8), (2, 0, 1, 0x00BFE0))
        self.assertEqual(t["widx"], ws["trigger"])            # lines up with the waveform trigger
        self.assertEqual(t["dt"], 36)
        e = P.log_entry(bytes.fromhex("0001234068 85 81 23"))
        self.assertEqual((e["addr"], e["siz"], e["rw"], e["region"], e["widx"], e["dt"]),
                         (0x00012340, 1, 1, 2, 0x185, 0x0123))

    def test_wave_duration(self):
        self.assertEqual(P.wave_duration(0x7FF), 2047)
        self.assertEqual(P.wave_duration(0x801), 256)
