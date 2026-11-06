import json

from tfdiag.tests import helpers
from tfdiag.tests import fakes
from tfdiag import events as E, fpga as F, spi as S, timing as T, protocol as P


class Timing(helpers.Base):
    def run_timing(self, presets=(0, 2, 5), expect_build=None, **kw):
        env = self.env
        ev = E.Events(stream=helpers.io.StringIO())
        spi = S.Spi(fakes.FakeSpiDev(env.state_path, env.clock))
        fp = F.Fpga(env.cfg, ev)
        info = T.load_info(env.images)
        out = env.tmp / "timing"
        tr = T.TimingRun(spi, fp, ev, out, env.images, info, clock=env.clock, secs=0.5, **kw)
        res = tr.run(list(presets), 0xFFFFFFFF, expect_build)
        return res, ev, out

    def test_full_run(self):
        res, ev, out = self.run_timing()
        self.assertTrue(res["ok"])
        self.assertEqual(res["diag_id"]["commit"], "0abc1234")
        self.assertEqual(sorted(res["histograms"]), ["0", "2", "5"])
        h = res["histograms"]["0"]["chip"]
        self.assertEqual((h["shift"], len(h["bins"]), len(h["bins"][0])), (dict(T.HIST_PLAN)["chip"], 8, 32))
        self.assertTrue(h["held"])
        self.assertGreater(h["windows"], 5)                          # short windows ...
        self.assertEqual(h["saturated_windows"], 0)                  # ... none fills a counter
        self.assertGreater(h["as"], 200_000)                         # target reached
        self.assertGreater(sum(h["bins"][3]), 0xFFFF)                # Pi-side totals exceed 16 bits
        for reg in ("cia", "rom", "ramsey", "other"):
            self.assertIn(reg, res["histograms"]["0"])
            self.assertTrue(res["histograms"]["0"][reg]["held"])
        trig = {(w["kind"], w["region_name"]) for w in res["waves"] if w["triggered"]}
        for k in ((3, "cia"), (3, "rom"), (3, "ramsey"), (4, "any")):
            self.assertIn(k, trig)
        self.assertTrue(res["hold_mode"])
        self.assertEqual(res["histogram_measurements"][2], "AS20 -> STERM (TA_BUS, no DSACK)")
        self.assertEqual(res["fabric_mhz"]["0"], 100.0)
        self.assertFalse(res["has_tprobe"])
        self.assertEqual(res["tprobe_histograms"], {})
        chip = res["cycles"]["0"]["chip"]
        self.assertGreater(len(chip), 50)                            # every logged cycle, not just triggers
        self.assertTrue(all(c.get("logged") and c["addr"].startswith("0x0001") for c in chip))
        self.assertIn(22.0, [c["as_to_dsack"] for c in chip])         # trigger cycles (fabric clks)
        self.assertEqual(sorted(c["as_to_dsack"] for c in res["cycles"]["0"]["cia"])[-1], 140.0)
        al = res["address_log"]
        self.assertTrue(al["used"])
        self.assertGreater(al["aligned"], 0)
        self.assertEqual(al["lost"], 0)
        w = next(w for w in res["waves"] if w["kind"] == 3 and w["region_name"] == "cia")
        lj = json.loads((out / w["log_file"]).read_text())
        trig = lj["status"]["trigger_entry"]
        self.assertEqual(lj["entries"][trig]["region"], "cia")
        self.assertEqual(lj["entries"][trig]["addr"][:8], "0x00bfe0")
        self.assertIn("ADDR", (out / w["file"]).read_text())
        self.assertEqual(self.board()["forbidden"], [])               # no 0xB4 before the body ran
        hj = json.loads((out / "histograms.json").read_text())
        self.assertEqual(sorted(hj), ["fabric_mhz", "histograms", "measurements"])
        self.assertEqual(res["machine"]["gary_id"], 0xD0)
        self.assertEqual([r for r in res["results"] if r["failure"]], [])
        self.assertEqual(res["clock_switches"], [{"preset": p, "acked": True, "polls": 1500 + 10 * p,
                                                  "bus_errors": p, "settled": True} for p in (0, 2, 5)])
        st = self.board()
        self.assertEqual(st.get("garbled", []), [])               # quiet while the PLL switched
        self.assertEqual(st.get("fast_during_switch", []), [])    # slow SPI during phase 0x0001
        self.assertEqual([a["preset"] for a in res["switch_announcements"]], [0, 2, 5])
        self.assertTrue(all(a["acked"] for a in res["switch_announcements"]))
        self.assertTrue(all(h["echoed"] for h in res["hold_requests"]))
        self.assertEqual(len(res["hold_requests"]), 3 * 6)
        self.assertTrue(all(h["phase"] not in (0x01, 0x03) for h in res["hold_requests"]))
        self.assertEqual(sorted(res["hold_echo"]), ["group", "loops", "seq"])
        self.assertEqual(res["hangs"], [])
        self.assertTrue(res["waves"])
        self.assertTrue((out / res["waves"][0]["file"]).read_text().startswith("$date"))
        self.assertTrue((out / "histograms.csv").read_text().startswith("preset,region"))
        json.loads((out / "timing.json").read_text())
        st = self.board()
        self.assertTrue(st["amiga_reset"])               # left held in reset
        self.assertEqual(st.get("exposures", []), [])   # released only for the exerciser
        statuses = {r["name"]: r["status"] for r in ev.results}
        self.assertEqual(statuses["clock_fabric"], "info")
        self.assertEqual(statuses["clock_CLKAUX"], "info")
        self.assertEqual(statuses["clock_pll_ratio"], "info")
        self.assertEqual(statuses["exerciser"], "info")
        self.assertEqual(st["forbidden"], [])

    def test_hang_resumes_at_next_preset(self):
        self.env.board.update(lambda s: s["ex"].update(hang_preset=2, errors=[[0, 0x30]]))
        res, ev, out = self.run_timing()
        loads = [l for l in self.board()["log"] if "-f" in l and "diag_timing" in l]
        self.assertEqual(len(loads), 2)                      # reloaded after the hang
        self.assertEqual(self.board().get("exposures", []), [])   # 0xA1 right after the reload
        self.assertEqual([r["preset"] for r in res["results"] if r["failure"]], [0])   # kept across the reload
        self.assertEqual([h["preset"] for h in res["hangs"]], [2])
        self.assertIn("5", res["histograms"])
        self.assertIsNotNone(res["machine"])
        self.assertTrue(any(r["name"] == "hang_preset_2" and r["status"] == "finding" for r in ev.results))

    def test_exerciser_errors_reported(self):
        self.env.board.update(lambda s: s["ex"].update(errors=[[2, 0x30]], info_result=True))
        res, ev, out = self.run_timing()
        fails = [r for r in res["results"] if r["failure"]]
        self.assertEqual(len(fails), 1)
        self.assertEqual((fails[0]["preset"], fails[0]["group"], fails[0]["got"]),
                         (2, "motherboard fast RAM", "0x55aa5daa"))
        self.assertTrue(any(not r["failure"] and "info" in r["flags"] for r in res["results"]))
        self.assertTrue(any(r["name"] == "exerciser_p2_motherboard_fast_RAM" for r in ev.results))

    def test_build_matches(self):
        self.assertTrue(T.build_matches("0abc1234", "abc1234"))
        self.assertTrue(T.build_matches("abc12345", "abc12345f00"))
        self.assertFalse(T.build_matches("0abc1234", "deadbee"))

    def test_ramsey_capture_attribution(self):
        from tfdiag.tests.fakes import FakeSpiDev
        from tfdiag import protocol as P
        tr = T.TimingRun(None, None, None, self.env.tmp / "t", self.env.images)
        ents = [(0xFFF, 10)] * 3 + FakeSpiDev._cycle(6, sterm=True) + FakeSpiDev._cycle(20)
        tr.add_cycles(2, 3, P.HIST_REGIONS["ramsey"], ents, 3)
        tr.add_cycles(2, 4, 0, ents, 3)                       # STERM trigger -> ramsey too
        rc = tr.cycles[2]["ramsey"]
        self.assertEqual([c["as_to_sterm"] for c in rc], [6.0, 6.0])

    def test_with_tprobe(self):
        self.env.board.update(lambda s: s.__setitem__("features", 0x6F))
        res, ev, out = self.run_timing()
        self.assertTrue(res["ok"])
        self.assertTrue(res["has_tprobe"])
        self.assertTrue(res["tprobe_histograms"])
        self.assertTrue(res["histograms"])                 # windows alternate
        self.assertTrue((out / "tprobe_histograms.csv").exists())

    def test_hold_write_order(self):
        from tfdiag import protocol as P
        writes = []

        class Rec:
            def sram_write(self, addr, data):
                writes.append((addr, bytes(data)))
        tr = T.TimingRun(Rec(), None, None, self.env.tmp / "t", self.env.images)
        seq = tr.set_hold(2)                                  # CIA: group 3
        self.assertEqual(writes, [(P.MAILBOX + 0x832, seq.to_bytes(2, "big")),
                                  (P.MAILBOX + 0x830, b"\x00\x03")])
        tr.set_hold(None)
        self.assertEqual(writes[-1], (P.MAILBOX + 0x830, b"\x00\x00"))

    def test_hold_waits_for_machine_id_and_switch(self):
        # a long machine ID group and slow clock switches: no hold request is sent during them
        self.env.board.update(lambda s: s["ex"].update(machine_id_ticks=300, switch_ticks=80))
        res, ev, out = self.run_timing(presets=(0, 2))
        self.assertTrue(res["hold_mode"])
        self.assertTrue(res["hold_requests"])
        self.assertTrue(all(h["echoed"] and h["phase"] not in (0x01, 0x03) for h in res["hold_requests"]))

    def test_slow_echo_is_not_an_older_body(self):
        # the first echo arrives only after a long group ends (later than the 5 s timeout):
        # that region is captured without hold, the rest are held
        self.env.board.update(lambda s: s["ex"].update(first_hold_delay=400))
        res, ev, out = self.run_timing(presets=(0, 2))
        self.assertTrue(res["hold_mode"])
        first = res["hold_requests"][0]
        self.assertFalse(first["echoed"])
        self.assertGreaterEqual(first["waited_s"], 5.0)
        self.assertTrue(any(h["echoed"] for h in res["hold_requests"] if h["preset"] == 0))
        self.assertTrue(all(h["echoed"] for h in res["hold_requests"] if h["preset"] == 2))

    def test_version_without_hold(self):
        # a version 1 body: no hold, no 0x088 announcements; slow SPI during its switches
        self.env.board.update(lambda s: s["ex"].update(version=1))
        res, ev, out = self.run_timing(presets=(0, 2))
        self.assertFalse(res["hold_mode"])
        self.assertEqual(res["hold_requests"], [])              # never asked
        self.assertEqual(res["switch_announcements"], [])       # no handshake
        self.assertTrue(res["ok"])
        st = self.board()
        self.assertEqual(st.get("garbled", []), [])             # slow SPI survives the switch
        self.assertEqual(st.get("fast_during_switch", []), [])
        self.assertTrue(all(not x["acked"] for x in res["clock_switches"]))

    def test_body_version_only_after_running_bit(self):
        T_ = T.TimingRun
        self.assertEqual(T_.body_version({"version": 1, "flags": 0}), 0)            # the stub's value
        self.assertEqual(T_.body_version({"version": 2, "flags": 0}), 0)
        self.assertEqual(T_.body_version({"version": 2, "flags": 0x80}), 2)
        res, ev, out = self.run_timing(presets=(0,))
        self.assertTrue(res["hold_mode"])                       # the body wrote 2 after bit 7

    def test_manifest_says_no_hold(self):
        m = json.loads((self.env.images / "diag_manifest.json").read_text())
        m["exerciser_hold"] = False
        (self.env.images / "diag_manifest.json").write_text(json.dumps(m))
        res, ev, out = self.run_timing(presets=(0,))
        self.assertEqual(res["hold_requests"], [])

    def test_switch_timeout_reported(self):
        self.env.board.update(lambda s: s["ex"].update(switch={"2": None}))
        res, ev, out = self.run_timing(presets=(0, 2))
        self.assertEqual([(x["preset"], x["settled"]) for x in res["clock_switches"]], [(0, True), (2, False)])

    def test_switch_without_handshake_garbles(self):
        """The fake reproduces the T hangs: without the ack the exerciser switches on its own
        and fast polling during the switch window turns into 0xA1 (the Amiga stops)."""
        env = self.env
        ev = E.Events(stream=helpers.io.StringIO())
        spi = S.Spi(fakes.FakeSpiDev(env.state_path, env.clock))
        class FastSpi(S.Spi):                                   # no slow SPI either
            slow = property(lambda self: False, lambda self, v: None)
        spi = FastSpi(fakes.FakeSpiDev(env.state_path, env.clock))
        tr = T.TimingRun(spi, F.Fpga(env.cfg, ev), ev, env.tmp / "t", env.images, T.load_info(env.images),
                         clock=env.clock, secs=0.5, switch_handshake=False)
        tr.run([0, 2], 0)
        self.assertTrue(self.board().get("garbled"))

    def test_switch_slow_ack_still_quiet(self):
        # the exerciser waits up to ~1 s for the ack (50 ticks here): tfdiag acks well before
        self.env.board.update(lambda s: s["ex"].update(ack_timeout_ticks=400, switch_window_s=0.04))
        res, ev, out = self.run_timing(presets=(0, 2))
        self.assertEqual(self.board().get("garbled", []), [])
        self.assertTrue(all(x["acked"] for x in res["clock_switches"]))

    def test_body_without_running_bit_gets_no_log(self):
        self.env.board.update(lambda s: s["ex"].__setitem__("old_body", True))
        res, ev, out = self.run_timing(presets=(0,))
        self.assertTrue(res["ok"])
        self.assertFalse(res["address_log"]["used"])
        self.assertEqual(self.board()["forbidden"], [])
        self.assertTrue(res["waves"])

    def test_align_log(self):
        from tfdiag import protocol as P
        ws = {"first": 500}
        log = [{"widx": 500}, {"widx": 3}, {"widx": 499}]
        by = T.TimingRun.align_log(log, ws, 20)
        self.assertEqual(sorted(by), [0, 15])                         # 499 is older than the ring
        self.assertEqual(by[15]["widx"], 3)
        self.assertEqual(P.WAVE_ENTRIES, 512)

    def test_older_body_without_hold(self):
        # an older body: no hold echo; its plan runs at its own pace (slower in this fake)
        self.env.board.update(lambda s: s["ex"].update(no_hold=True, ticks_per_phase=200))
        res, ev, out = self.run_timing()
        self.assertTrue(res["ok"])
        self.assertFalse(res["hold_mode"])
        self.assertTrue(res["histograms"])                          # round-robin windows as before
        self.assertFalse(any(v.get("held") for regs in res["histograms"].values() for v in regs.values()))
        self.assertIsNotNone(res["machine"])

    def test_without_histograms(self):
        self.env.board.update(lambda s: s.__setitem__("features", 0x0E))
        res, ev, out = self.run_timing()
        self.assertTrue(res["ok"])
        self.assertEqual(res["histograms"], {})
        self.assertEqual(res["cycle_counters"], {})        # bit6 clear: not read
        self.assertTrue(res["cycles"])

    def test_cycle_counters(self):
        res, ev, out = self.run_timing()
        self.assertEqual(sorted(res["cycle_counters"]), ["0", "2", "5"])
        self.assertEqual(res["cycle_counters"]["0"]["sterm"], 300)

    def test_no_body_retry(self):
        self.env.board.update(lambda s: s["ex"].__setitem__("no_body_once", True))
        res, ev, out = self.run_timing(presets=(0,))
        self.assertTrue(res["ok"])
        self.assertIsNotNone(res["machine"])

    def test_wrong_build(self):
        res, ev, out = self.run_timing(presets=(0,), expect_build="deadbee")
        self.assertFalse(res["ok"])

    def test_bad_image_checksum(self):
        (self.env.images / "diag_timing.bin").write_bytes(helpers.PREAMBLE + b"x")
        with self.assertRaises(F.FpgaError):
            self.run_timing()

    def test_clock_off_frequency(self):
        self.env.board.update(lambda s: s.__setitem__("fabric_scale", 1.05))
        res, ev, out = self.run_timing(presets=(0,))
        st = {r["name"]: r["status"] for r in ev.results}
        self.assertEqual(st["clock_fabric"], "finding")
        self.env.board.update(lambda s: (s.__setitem__("fabric_scale", 1.0), s.__setitem__("aux_scale", 0.9)))
        res, ev, out = self.run_timing(presets=(0,))
        st = {r["name"]: r["status"] for r in ev.results}
        self.assertEqual((st["clock_fabric"], st["clock_CLKAUX"], st["clock_pll_ratio"]), ("info", "finding", "finding"))

    def test_protocol_version_mismatch(self):
        import json
        m = json.loads((self.env.images / "diag_manifest.json").read_text())
        m["spi_protocol"] = 2
        (self.env.images / "diag_manifest.json").write_text(json.dumps(m))
        res, ev, out = self.run_timing(presets=(0,))
        self.assertFalse(res["ok"])

    def test_hist_stats(self):
        bins = [3] + [0] * 31
        bins[2] = 10
        bins[31] = 1
        st = T.hist_stats(0, bins, 100.0)
        self.assertEqual(st["max_lo_ns"], 150.0)
        self.assertEqual((st["count"], st["special"], st["min_ns"]), (11, 3, 5.0))
        self.assertTrue(st["max_open"])
        self.assertAlmostEqual(T.frac_above(0, bins, 150.0, 100.0), 1 / 11)
        ph = T.hist_stats(16, [0] + [1] * 31, 100.0)
        self.assertEqual(len(ph["phase"]), P.AMIGA_PERIOD_HALF)
