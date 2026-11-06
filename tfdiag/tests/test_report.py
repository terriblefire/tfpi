import json
import os
import subprocess
import sys
import zipfile

from tfdiag.tests import helpers
from tfdiag import report, bundle


class Report(helpers.Base):
    def job(self, **ex):
        env = self.env
        if ex:
            env.board.update(lambda s: s["ex"].update(ex))
        d = env.tmp / "job"
        rc, evs = helpers.run_cli(env, ["run", "--images", str(env.images), "--out", str(d),
                                        "--presets", "0,2", "--secs", "0.2"])
        return d, rc, evs

    def test_report_contents(self):
        d, rc, evs = self.job(stale_dsack_preset=2)
        self.assertEqual(rc, 0, [e for e in evs if e.get("status") in ("fail", "incomplete")])
        txt = (d / "report.txt").read_text()
        html = (d / "report.html").read_text()
        self.assertRegex(txt, r"AS20 fall -> stale DSACK release \(histogram\): p0: none, \d+ not stale; "
                              r"p2: median 400.0 ns \(400.0..400.0, n=\d+\), \d+ not stale")
        self.assertNotIn("saturated", txt)
        self.assertIn("(exerciser held in this group)", txt)
        self.assertIn("Clock switch (CLKSEL): p0: 1500 polls, 0 bus errors, acked; p2: 1520 polls, 2 bus errors, acked", txt)
        self.assertIn("DS20 -> DSACK, reads (histogram): p0: median 1360.0 ns", txt)
        self.assertIn("== Motherboard fast RAM: Ramsey", txt)
        self.assertIn("AS20 -> STERM (TA_BUS, no DSACK) (histogram): p0: median 60.0 ns", txt)
        self.assertIn("Fabric clock per preset: p0: 100.0 MHz", txt)
        self.assertTrue((d / "timing" / "histograms.csv").exists())
        self.assertNotIn("[WARN]", txt)
        self.assertIn("Measurements only", txt)
        cia = txt[txt.index("== CIAs"):txt.index("== Kickstart ROM")]
        self.assertRegex(cia, r"Stale DSACK \(waveform\): p0: 0/\d+; p2: [1-9]\d*/\d+")   # several captures, held
        self.assertIn("== CIAs: 8520 CIA-A/CIA-B via Gary/Fat Gary", txt)
        self.assertIn("AS20 -> DSACK, reads (waveform): p0: 1400.0", txt)
        self.assertIn("AS20 -> STERM, reads (waveform)", txt)
        self.assertLess(txt.index("== Machine"), txt.index("== Chip RAM"))
        self.assertIn("[RESTORED]   Restore check", txt)
        self.assertIn("Interconnect: 56/56 nets follow 0/1", txt)
        self.assertIn("CPLD IDCODE / usercode", txt)
        self.assertIn("Gary / Fat Gary: ID 0xd0", txt)
        self.assertEqual(txt.count("== Motherboard fast RAM"), 1)
        self.assertIn("prefers-color-scheme", html)
        self.assertIn("<svg", html)
        rows = report.rows_for(report.collect(d))
        self.assertTrue(all(r[4] in ("info", "finding", "incomplete", "pass", "fail") for r in rows))
        timing = [r for r in rows if any(k in r[1] for k in ("(waveform)", "(histogram)", "(cycle counters)"))]
        self.assertTrue(timing)
        self.assertTrue(all(r[4] == "info" and not r[3] for r in timing))   # measurements only

    def test_bundle_scrubs(self):
        d, rc, evs = self.job()
        (d / "events.jsonl").write_text('{"msg":"ssh pi@192.168.10.39 to pifpga.local, host tfpi-cr"}\n')
        out = self.env.tmp / "bundle.zip"
        from tfdiag import vcdsvg
        v = next((d / "timing" / "waves").glob("*.vcd"))
        vcdsvg.convert(v)                                       # as the tab does
        (d / "timing" / "waves" / "host.svg").write_text("<svg><text>pifpga.local</text></svg>")
        contents = bundle.make(d, out, hostnames={"tfpi-cr"})
        with zipfile.ZipFile(out) as z:
            ev = z.read("events.jsonl").decode()
            names = z.namelist()
        self.assertNotIn("192.168", ev)
        self.assertNotIn("pifpga", ev)
        self.assertNotIn("tfpi-cr", ev)
        for n in ("report.html", "report.txt", "before.json", "restore.json", "board/board.json",
                  "timing/timing.json", "timing/histograms.json", "timing/histograms.csv"):
            self.assertIn(n, names)
        self.assertTrue(any(n.endswith(".vcd") for n in names))
        self.assertTrue(any(n.endswith(".log.json") for n in names))
        self.assertIn("timing/waves/" + v.with_suffix(".svg").name, names)
        with zipfile.ZipFile(out) as z:
            self.assertNotIn("pifpga", z.read("timing/waves/host.svg").decode())
        self.assertEqual(len(contents), len(names))


class Cli(helpers.Base):
    def test_module_entry(self):
        env = {**os.environ, "PYTHONPATH": str(helpers.PI)}
        r = subprocess.run([sys.executable, "-m", "tfdiag", "--help"], capture_output=True, text=True, env=env)
        self.assertEqual(r.returncode, 0)
        self.assertIn("snapshot", r.stdout)
        d = self.env.tmp / "empty"
        d.mkdir()
        r = subprocess.run([sys.executable, "-m", "tfdiag", "report", "--dir", str(d)], capture_output=True,
                           text=True, env=env)
        lines = [json.loads(l) for l in r.stdout.splitlines()]
        self.assertEqual(lines[-1]["event"], "done")
        self.assertEqual(r.returncode, 0)
