import json
import os
import subprocess
import sys
import unittest
from dataclasses import asdict
from pathlib import Path

from tfdiag.tests import helpers
from tfdiag import session as S, events as E


def restored(t, s):
    t.assertEqual(s["image"], "flash")
    t.assertFalse(s["jtag"]["extest"])
    t.assertEqual(s["jtag"]["ir"], "IDCODE")
    t.assertEqual(s["a314d"], "active")
    t.assertFalse(s["amiga_reset"])
    t.assertEqual(s["forbidden"], [])
    t.assertEqual(s["flash_writes"], 0)


class Session(helpers.Base):
    def test_snapshot_restore(self):
        env = self.env
        rc, evs = helpers.run_cli(env, ["snapshot", "--out", str(env.tmp / "before.json")])
        self.assertEqual(rc, 0, evs)
        b = json.loads((env.tmp / "before.json").read_text())
        self.assertEqual(b["flash_jedec"], "bf2641")
        self.assertEqual(b["cpld_idcode"], "0x59616093")
        self.assertTrue(b["a314d_active"])
        self.assertTrue(Path(env.cfg.marker).exists())
        s = self.board()
        self.assertEqual(s["a314d"], "inactive")
        self.assertEqual(s["image"], "diag_timing")  # flash read, then diag_timing.bin again ...
        self.assertTrue(s["amiga_reset"])            # ... with the Amiga held in reset
        self.assertEqual(s.get("boots", []), [])     # AmigaOS never ran
        self.assertEqual(s.get("exposures", []), [])
        self.assertEqual(json.loads((env.tmp / "before.json").read_text())["held_after_snapshot"],
                         "diag_timing.bin")
        self.assertEqual(evs[-1]["event"], "done")
        rc, evs = helpers.run_cli(env, ["restore", "--before", str(env.tmp / "before.json"),
                                        "--out", str(env.tmp / "restore.json")])
        self.assertEqual(rc, 0, evs)
        r = json.loads((env.tmp / "restore.json").read_text())
        self.assertTrue(r["ok"])
        self.assertEqual(r["differences"], [])
        self.assertFalse(Path(env.cfg.marker).exists())
        restored(self, self.board())

    def test_board_and_timing_need_snapshot(self):
        rc, evs = helpers.run_cli(self.env, ["board", "--images", str(self.env.images), "--out", "x"])
        self.assertEqual(rc, 2)
        self.assertIn("snapshot", evs[-1]["summary"])

    def assert_never_exposed(self, boots=1):
        """After the first diag load the Amiga was never out of reset with a diag image unless
        the Pi released it for the exerciser, and AmigaOS booted only at the final restore."""
        s = self.board()
        self.assertEqual(s.get("exposures", []), [])
        self.assertEqual(len(s.get("boots", [])), boots)
        if boots:
            self.assertEqual(s["image"], "flash")

    def test_snapshot_without_images_falls_back(self):
        env = self.env
        env.cfg.images = str(env.tmp / "nothing")
        rc, evs = helpers.run_cli(env, ["snapshot", "--out", str(env.tmp / "before.json")])
        self.assertEqual(rc, 0)
        self.assertTrue(any("no diag_timing.bin or diag_pins.bin" in e.get("msg", "") for e in evs))
        self.assertEqual(self.board()["image"], "flash")

    def test_tab_sequence(self):
        env, d = self.env, self.env.tmp / "job"
        rc, _ = helpers.run_cli(env, ["snapshot", "--out", str(d / "before.json")])
        self.assertEqual(rc, 0)
        rc, evs = helpers.run_cli(env, ["board", "--images", str(env.images), "--out", str(d / "board")])
        self.assertEqual(rc, 0, [e for e in evs if e.get("status") == "fail"])
        self.assertEqual(self.board()["image"], "diag_pins")
        rc, evs = helpers.run_cli(env, ["timing", "--images", str(env.images), "--out", str(d / "timing"),
                                        "--presets", "0,2", "--secs", "0.2"])
        self.assertEqual(rc, 0, [e for e in evs if e.get("status") == "fail"])
        rc, evs = helpers.run_cli(env, ["restore", "--before", str(d / "before.json"),
                                        "--out", str(d / "restore.json")])
        self.assertEqual(rc, 0)
        restored(self, self.board())
        self.assert_never_exposed()
        rc, evs = helpers.run_cli(env, ["report", "--dir", str(d)])
        self.assertEqual(rc, 0, evs[-1])
        self.assertTrue((d / "report.html").exists())
        rc, evs = helpers.run_cli(env, ["bundle", "--dir", str(d), "--out", str(env.tmp / "b.zip")])
        names = [c["name"] for c in evs[-1]["contents"]]
        self.assertIn("report.html", names)
        self.assertIn("timing/histograms.csv", names)
        for e in evs:
            self.assertIn(e["event"], {"step", "progress", "log", "result", "done"})

    def test_run_finds_faults_and_restores(self):
        env = self.env
        env.board.update(lambda s: s.__setitem__("shorts", [["TS", "SIZ60_0"]]))
        rc, evs = helpers.run_cli(env, ["run", "--images", str(env.images), "--out", str(env.tmp / "r"),
                                        "--presets", "0", "--secs", "0.2"])
        self.assertEqual(rc, 0)                     # capture complete: findings are data, not failures
        fails = [e["name"] for e in evs if e["event"] == "result" and e["status"] == "finding"]
        self.assertIn("net_TS", fails)
        self.assertGreaterEqual(evs[-1]["findings"], 1)
        done = evs[-1]
        self.assertEqual(done["event"], "done")
        self.assertTrue(done["restore_ok"])
        restored(self, self.board())
        self.assertFalse(Path(env.cfg.marker).exists())
        self.assert_never_exposed()
        txt = (env.tmp / "r" / "report.txt").read_text()
        self.assertIn("[FINDING]", txt)
        self.assertIn("net TS", txt)

    def test_incompatible_cpld_skips_timing(self):
        env = self.env
        import json as _j
        m = _j.loads((env.images / "diag_manifest.json").read_text())
        m["cpld_usercodes"] = ["0x11111111"]
        (env.images / "diag_manifest.json").write_text(_j.dumps(m))
        rc, _ = helpers.run_cli(env, ["snapshot", "--out", str(env.tmp / "before.json")])
        rc, evs = helpers.run_cli(env, ["timing", "--images", str(env.images), "--out", str(env.tmp / "t")])
        self.assertEqual(rc, 2)
        self.assertEqual(self.board()["image"], "diag_pins")         # held by the pin image ...
        self.assertNotIn("diag_timing", " ".join(self.board()["log"]))   # ... the timing image never loaded
        self.assertNotIn("diag_timing", " ".join(self.board()["log"]))
        rc, evs = helpers.run_cli(env, ["board", "--images", str(env.images), "--out", str(env.tmp / "b")])
        self.assertEqual(rc, 0)                                       # board test still allowed
        rc, evs = helpers.run_cli(env, ["run", "--images", str(env.images), "--out", str(env.tmp / "r")])
        self.assertEqual(rc, 2)                                       # marker from the snapshot above
        rc, evs = helpers.run_cli(env, ["restore"])
        self.assertEqual(rc, 0)
        rc, evs = helpers.run_cli(env, ["run", "--images", str(env.images), "--out", str(env.tmp / "r")])
        self.assertEqual(rc, 1)                                       # capture incomplete: no timing
        self.assertTrue(any(e.get("name") == "cpld_compatibility" and e["status"] == "incomplete" for e in evs))
        self.assertFalse((env.tmp / "r" / "timing").exists())
        restored(self, self.board())

    def test_flash_change_is_caught(self):
        env = self.env
        rc, _ = helpers.run_cli(env, ["snapshot", "--out", str(env.tmp / "before.json")])
        env.board.update(lambda s: s.__setitem__("flash_tag", 99))      # something rewrote the flash
        rc, evs = helpers.run_cli(env, ["restore", "--before", str(env.tmp / "before.json"),
                                        "--out", str(env.tmp / "restore.json")])
        self.assertEqual(rc, 1)
        r = json.loads((env.tmp / "restore.json").read_text())
        self.assertFalse(r["ok"])
        self.assertTrue(any("checksum" in d for d in r["differences"]))
        self.assertIn("re-flash", r["advice"])
        self.assertTrue(Path(env.cfg.marker).exists())                 # kept for another try

    def test_board_lost_mid_test(self):
        env = self.env
        rc, _ = helpers.run_cli(env, ["snapshot", "--out", str(env.tmp / "before.json")])
        env.board.update(lambda s: (s.__setitem__("powered", False), s.__setitem__("power_return_after", 3)))
        hw = env.hw()
        res = S.restore(env.cfg, hw, E.Quiet(), before=None, out=env.tmp / "restore.json", sleep=lambda s: None)
        self.assertTrue(res["ok"], res)
        restored(self, self.board())

    def test_board_never_returns(self):
        env = self.env
        rc, _ = helpers.run_cli(env, ["snapshot", "--out", str(env.tmp / "before.json")])
        env.board.update(lambda s: s.__setitem__("powered", False))
        env.cfg.board_wait = 0.2
        res = S.restore(env.cfg, env.hw(), E.Quiet(), sleep=lambda s: None)
        self.assertFalse(res["ok"])
        self.assertIn("power the Amiga on", res["advice"])
        self.assertTrue(Path(env.cfg.marker).exists())

    def test_recover_without_marker(self):
        rc, evs = helpers.run_cli(self.env, ["recover"])
        self.assertEqual(rc, 0)
        self.assertEqual(self.board()["log"], [])          # touched nothing

    def test_lock(self):
        import fcntl
        Path(self.env.cfg.flash_lock).parent.mkdir(parents=True, exist_ok=True)
        with open(self.env.cfg.flash_lock, "w") as h:
            fcntl.flock(h, fcntl.LOCK_EX)
            rc, evs = helpers.run_cli(self.env, ["snapshot"])
        self.assertEqual(rc, 2)
        self.assertIn("firmware flash", evs[-1]["summary"])


class PowerCut(helpers.Base):
    """Kill `tfdiag run` at every checkpoint (os._exit: no finally, no cleanup), then
    recover: the board must come back and the check must pass."""

    def spawn(self, kill, extra_state=None):
        env = self.env
        d = {"state_path": str(env.state_path), "images": str(env.images), "statedir": str(env.statedir),
             "cfg": asdict(env.cfg)}
        (env.tmp / "env.json").write_text(json.dumps(d))
        pp = str(helpers.PI) + os.pathsep + os.environ.get("PYTHONPATH", "")
        return subprocess.run([sys.executable, "-m", "tfdiag.tests.run_session", str(env.tmp), kill,
                               "run", "--images", str(env.images), "--out", str(env.tmp / "out"),
                               "--presets", "0,2", "--secs", "0.2"],
                              env={**os.environ, "PYTHONPATH": pp}, capture_output=True, text=True, timeout=300)

    def test_power_cut_at_every_checkpoint(self):
        r = self.spawn("record")
        self.assertEqual(r.returncode, 0, r.stderr[-2000:])
        points = (self.env.tmp / "checkpoints.txt").read_text().split()
        for must in ("snapshot.marker#1", "snapshot.a314d_stopped#1", "snapshot.flash_read#1",
                     "board.timing_image#1", "board.pins_image#1", "board.extest_mid#1", "timing.image#1",
                     "timing.started#1", "restore.tlr#1", "restore.fpga#1", "restore.a314d#1"):
            self.assertIn(must, points)
        clean = Path(self.env.state_path).read_text()
        for kill in points:
            with self.subTest(kill=kill):
                Path(self.env.state_path).write_text(clean)
                helpers.fakes.State(self.env.state_path).update(lambda s: s.__setitem__("log", []))
                try:
                    os.unlink(self.env.cfg.marker)
                except FileNotFoundError:
                    pass
                r = self.spawn(kill)
                self.assertEqual(r.returncode, 137, r.stderr[-2000:])
                # the Pi reboots (a power cut takes the Pi with it); a314d is enabled at boot,
                # so systemd may or may not have started it before recover runs
                rc, evs = helpers.run_cli(self.env, ["recover", "--out", str(self.env.tmp / "rec.json")])
                self.assertEqual(rc, 0, [e for e in evs if e["event"] in ("result", "done")])
                s = self.board()
                restored(self, s)
                self.assertFalse(Path(self.env.cfg.marker).exists())

    def test_recover_does_not_block_on_a314d(self):
        """Boot-time recovery runs Before=a314d: it must queue the a314d start (--no-block),
        never wait for it (that deadlocked on the T until systemctl's 60 s timeout)."""
        r = self.spawn("timing.started#1")
        self.assertEqual(r.returncode, 137)
        self.env.board.update(lambda s: (s.__setitem__("a314d", "inactive"), s.__setitem__("log", [])))
        res = S.recover(self.env.cfg, self.env.hw(), E.Quiet(), sleep=lambda s: None)
        self.assertTrue(res["ok"], res)
        starts = [l for l in self.board()["log"] if l.startswith("systemctl start")]
        self.assertEqual(starts, ["systemctl start --no-block a314d"])
        self.assertFalse(Path(self.env.cfg.marker).exists())

    def test_power_cut_with_board_off(self):
        """Board power lost too: it comes back on its own image after a few polls."""
        r = self.spawn("board.extest_mid#1")
        self.assertEqual(r.returncode, 137)
        self.env.board.update(lambda s: (s.__setitem__("powered", False), s.__setitem__("power_return_after", 4),
                                         s.__setitem__("a314d", "inactive")))
        self.env.cfg.board_wait = 30
        res = S.recover(self.env.cfg, self.env.hw(), E.Quiet(), sleep=lambda s: None)
        self.assertTrue(res["ok"], res)
        restored(self, self.board())


if __name__ == "__main__":
    unittest.main()
