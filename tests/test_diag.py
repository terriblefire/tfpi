import configparser
import fcntl
import hashlib
import io
import json
import os
import re
import sys
import textwrap
import time
import zipfile
from pathlib import Path

from helpers import ROOT, Sandbox, full_image, jed, make_zip
from tfpi import config, diag, flasher

# Stand-in for `python3 -m tfdiag` (the real package has its own tests under
# tfdiag/tests, run by test_tfdiag.py). Same CLI, events and files; FAKE_DIAG_*
# steer it. Holds the flash lock like tfdiag does.
TFDIAG_STUB = r'''
import fcntl, json, os, signal, sys, time
from pathlib import Path
cmd, args = sys.argv[1], sys.argv[2:]
opt = lambda name: args[args.index(name) + 1] if name in args else None
log = os.environ.get("FAKE_DIAG_LOG")
if log:
    open(log, "a").write(" ".join(sys.argv[1:]) + "\n")
def ev(**k): print(json.dumps(dict(k, t=time.time())), flush=True)
state = Path(os.environ["TFPI_STATE_DIR"])
marker = state / "diag-in-progress"
held = open(state / "flash.lock", "a")
fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)

def restore(out):
    bad = bool(os.environ.get("FAKE_DIAG_RESTORE_BAD"))
    res = {"ok": not bad, "differences": ["CPLD usercode differs"] if bad else [],
           "advice": "The CPLD answers differently." if bad else "ok"}
    out.write_text(json.dumps(res))
    if not bad:
        marker.unlink(missing_ok=True)
    ev(event="result", step="restore", name="restore", status="fail" if bad else "pass", msg="restore")
    return not bad

class Abort(Exception):
    pass

def raise_abort(*_):
    raise Abort()

if cmd == "run":
    out = Path(opt("--out"))
    out.mkdir(parents=True, exist_ok=True)
    signal.signal(signal.SIGTERM, raise_abort)
    phases = [p for p in ("board", "timing") if "--no-" + p not in args]
    incomplete = False
    try:
        ev(event="step", step="snapshot", msg="recording the state before the test")
        before = {"serial": "00000000feedf00d", "flash_jedec": "bf2641"}
        (out / "before.json").write_text(json.dumps(before))
        marker.write_text(json.dumps({"before": before, "before_file": str(out / "before.json")}))
        for phase in phases:
            step = "jtag" if phase == "board" else "timing"
            ev(event="step", step=step, msg="starting " + phase)
            if os.environ.get("FAKE_DIAG_HANG") == phase:
                while True:
                    ev(event="progress", pct=10, msg="waiting for the exerciser")
                    time.sleep(0.1)
            (out / phase).mkdir(exist_ok=True)
            if phase == "timing":
                (out / "timing" / "timing.json").write_text(json.dumps({"args": args}))
                (out / "timing" / "as_fall.vcd").write_text("$timescale 1ns $end\n")
                (out / "report.html").write_text("<p>Pi 192.168.1.20 fe80::1 on stub-pi.local at 12:34:56</p>"
                                                 "<script>window.boardview = 1</script>")
                if os.environ.get("FAKE_DIAG_INCOMPLETE") == phase:
                    ev(event="result", step="timing", name="exerciser_start", status="incomplete",
                       msg="the exerciser did not start")
                    incomplete = True
                else:
                    ev(event="result", step="timing", name="clock_fabric", status="info", msg="100.0 MHz")
            else:
                (out / "board" / "board.json").write_text("{}")
                print("plain text line from openocd")
                ev(event="result", step="jtag", name="cpld_idcode", status="info", msg="IDCODE 0x59616093")
                if os.environ.get("FAKE_DIAG_FINDING") == phase:
                    ev(event="result", step="extest", name="net_TS", status="finding",
                       msg="shorted to SIZ60_0 (CPLD pin 12)")
    except Abort:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        ev(event="log", level="warn", msg="interrupted (SIGTERM): restoring")
        ev(event="result", step="timing", name="interrupted", status="incomplete", msg="stopped early")
        incomplete = True
    ok = restore(out / "restore.json")
    ev(event="done", ok=ok and not incomplete, summary="capture complete", restore_ok=ok,
       complete=not incomplete)
    sys.exit(0 if ok and not incomplete else 1)
elif cmd == "identify":
    out = Path(opt("--out"))
    out.mkdir(parents=True, exist_ok=True)
    marker.write_text("{}")
    ev(event="step", step="snapshot", msg="recording the state before the test")
    ok = restore(out / "restore.json")
    verdict = {"fpga": {"state": "unknown", "git": "beef123", "built": "2026-11-06",
                        "detail": "not a known release: built from git beef123 on 2026-11-06 (boot ROM)"},
               "cpld": {"state": "known", "release": "tf4060r5_2026-11-05_f555529",
                        "detail": "design of release tf4060r5_2026-11-05_f555529"}}
    (out / "firmware.json").write_text(json.dumps({"time": "2026-11-07 11:00:00", "firmware": {},
                                                   "verdict": verdict, "restore_ok": ok}))
    ev(event="done", ok=ok, summary="firmware read back", restore_ok=ok, verdict=verdict)
    sys.exit(0 if ok else 1)
elif cmd == "recover":
    ok = restore(Path(opt("--out")))
    ev(event="done", ok=ok, summary="recovered" if ok else "not restored")
    sys.exit(0 if ok else 1)
sys.exit(2)
'''

IMAGES = {"diag_timing.bin": b"T" * 64, "diag_pins.bin": b"P" * 64, "diagex_body.bin": b"X" * 32}


def write_images(folder, manifest=True, **extra):
    folder.mkdir(parents=True, exist_ok=True)
    for name, data in IMAGES.items():
        (folder / name).write_bytes(data)
    if manifest:
        data = {"images": {n: hashlib.sha256(d).hexdigest() for n, d in IMAGES.items()},
                "build": "abc1234", "spi_protocol": 1, "cpld_usercodes": ["0x54463630"],
                "presets": [0, 2, 5]}
        data.update(extra)
        (folder / "diag_manifest.json").write_text(json.dumps(data))


class DiagBase(Sandbox):
    def setUp(self):
        super().setUp()
        self.diag_log = self.tmp / "tfdiag.log"
        self.images = self.tmp / "diag_images"
        stub = self.tmp / "tfdiag-stub"
        stub.write_text(f"#!{sys.executable}\n" + textwrap.dedent(TFDIAG_STUB))
        stub.chmod(0o755)
        extra = {"FAKE_DIAG_LOG": str(self.diag_log), "TFPI_CPUINFO": str(self.tmp / "cpuinfo"),
                 "TFPI_TFDIAG": str(stub), "TFPI_DIAG_IMAGES": str(self.images)}
        for key in ("FAKE_DIAG_HANG", "FAKE_DIAG_FINDING", "FAKE_DIAG_INCOMPLETE", "FAKE_DIAG_RESTORE_BAD"):
            self._saved.setdefault(key, os.environ.get(key))
            os.environ.pop(key, None)
        for key, value in extra.items():
            self._saved.setdefault(key, os.environ.get(key))
            os.environ[key] = value
        (self.tmp / "cpuinfo").write_text("Serial\t\t: 00000000deadbeef\n")
        diag.CPUINFO = Path(os.environ["TFPI_CPUINFO"])
        write_images(self.images)

    def calls(self):
        return [line.split()[0] for line in self.diag_log.read_text().splitlines()] if self.diag_log.exists() else []


class DiagWebBase(DiagBase):
    def setUp(self):
        super().setUp()
        from tfpi.web import create_app
        self.client = create_app().test_client()
        token = self.csrf("/setup")
        self.client.post("/setup", data={"csrf": token, "password": "secret1", "confirm": "secret1"})

    def csrf(self, path="/"):
        page = self.client.get(path).get_data(as_text=True)
        return re.search(r'name="csrf" value="([0-9a-f]+)"', page).group(1)

    def start(self, action="all", **form):
        data = {"csrf": self.csrf(), "action": action}
        data.update(form)
        return self.client.post("/diag/run", data=data)

    def wait(self, job, want="done", tries=150):
        reply = None
        for _ in range(tries):
            reply = self.client.get(f"/job/{job}/log").get_json()
            if reply["status"] == want:
                return reply
            time.sleep(0.1)
        self.fail(f"job {job} never reached {want}: {reply}")

    def wait_running(self):
        for _ in range(100):
            if diag.running() and diag.marker_file().exists():
                return
            time.sleep(0.05)
        self.fail("diag never started")


class VerifyTests(DiagWebBase):
    def test_verify_button_reads_back_and_shows_the_result(self):
        page = self.client.get("/firmware").get_data(as_text=True)
        self.assertIn('action="/verify"', page)
        self.assertIn("Verify reads the FPGA&#39;s flash and the CPLD back from the chips", page)
        reply = self.client.post("/verify", data={"csrf": self.csrf()})
        job = reply.headers["Location"].rsplit("/", 1)[1]
        self.assertTrue(job.startswith("verify-"))
        self.assertIn("Verifying the firmware", self.client.get(f"/job/{job}").get_data(as_text=True))
        done = self.wait(job)
        self.assertTrue(done["result"]["ok"], done)
        self.assertEqual(done["result"]["kind"], "verify")
        self.assertIn("identify", self.calls())
        self.assertFalse(diag.marker_file().exists())
        self.assertFalse(diag.current_file().exists())
        page = self.client.get("/firmware").get_data(as_text=True)
        self.assertIn("built from git beef123 on 2026-11-06", page)
        self.assertIn("tf4060r5_2026-11-05_f555529", page)
        self.assertIn("Verified</th><td>2026-11-07 11:00:00", page)

    def test_verify_refused_while_a_test_is_pending(self):
        diag.marker_file().parent.mkdir(parents=True, exist_ok=True)
        diag.marker_file().write_text("{}")
        reply = self.client.post("/verify", data={"csrf": self.csrf()})
        self.assertEqual(reply.headers["Location"], "/firmware")
        self.assertNotIn("identify", self.calls())


class TabTests(DiagWebBase):
    def test_tab_renders_with_warning(self):
        page = self.client.get("/diag").get_data(as_text=True)
        self.assertIn(diag.WARNING, page)
        self.assertIn("Do not power off during a test.", page)
        self.assertIn("screen will go blank and may change colour. This is normal.", page)
        self.assertIn("<b>Board test (experimental)</b>", page)
        self.assertIn(diag.BOARD_EXPERIMENTAL, page)
        self.assertIn('href="/diag">Diagnostics</a>', page)
        self.assertIn("Run all", page)
        self.assertIn("build <span class=\"mono\">abc1234", page)
        self.assertIn("5: 75 MHz (half bus)", page)           # presets from the manifest
        self.assertIn("mbram", page)                          # tests from tfdiag's exerciser bits
        self.assertNotIn("Tests can", page)

    def test_board_experimental_matches_tfdiag(self):
        from tfdiag import board
        self.assertEqual(diag.BOARD_EXPERIMENTAL, board.EXPERIMENTAL)

    def test_warning_text_is_exact(self):
        self.assertEqual(diag.WARNING, "This page is for diagnostic capture only. Tests temporarily load "
                         "diagnostic firmware and will reset your Amiga. Save your work first. When the "
                         "tests finish, the machine is returned to its original state.")

    def test_images_not_installed(self):
        for path in self.images.iterdir():
            path.unlink()
        page = self.client.get("/diag").get_data(as_text=True)
        self.assertIn("Diagnostic images not installed", page)
        self.assertIn("<button disabled>Start…</button>", page)
        self.assertIn("/diag", self.start().headers["Location"])
        self.assertEqual(self.calls(), [])

    def test_image_checksum_mismatch(self):
        (self.images / "diag_pins.bin").write_bytes(b"damaged")
        self.assertIn("doesn't match its checksum", diag.images_status()["reason"])

    def test_confirmation_repeats_warning_and_first_step(self):
        page = self.client.post("/diag/start", data={"csrf": self.csrf(), "action": "timing",
                                                     "preset": ["2", "5"], "test": ["0"]}).get_data(as_text=True)
        self.assertIn(diag.WARNING, page)
        self.assertIn("Do not power off during a test.", page)
        self.assertIn("the Amiga crashes or resets at that moment", page)
        self.assertIn("screen will go blank and may change colour", page)
        self.assertIn("62.5 MHz (half bus)", page)
        self.assertIn('name="tests" value="0x00000001"', page)
        self.assertIn("Discord / forum handle (optional)", page)
        self.assertIn("What's the problem? (optional)", page)
        self.assertIn("localStorage", page)
        self.assertEqual(self.calls(), [])


class RunTests(DiagWebBase):
    def test_run_all_restores_and_downloads(self):
        reply = self.start("all", preset=["0", "5"], handle="  amigafan\x07 ", note="Hangs at 75 MHz\r\nafter\x1b[0m boot")
        job = reply.headers["Location"].rsplit("/", 1)[1]
        self.assertTrue(job.startswith("diag-"))
        self.assertIn("Do not power off during a test.", self.client.get(f"/job/{job}").get_data(as_text=True))
        done = self.wait(job)
        self.assertTrue(done["result"]["ok"], done)
        self.assertTrue(done["result"]["restored"])
        self.assertEqual(done["result"]["result"],
                         "Capture complete - download the zip and send it to us; restored OK")
        self.assertEqual(self.calls(), ["run"])
        log = done["text"]
        self.assertIn("== jtag: starting board", log)        # JSON lines rendered
        self.assertIn("cpld_idcode: IDCODE 0x59616093", log)
        self.assertIn("plain text line from openocd", log)
        self.assertIn("OK restore: restore", log)
        self.assertIn("Restored: OK", log)
        self.assertNotIn("PASS", log)
        args = json.loads((diag.runs_dir() / job / "timing" / "timing.json").read_text())["args"]
        self.assertEqual(args[args.index("--presets") + 1], "0,5")
        self.assertEqual(args[args.index("--images") + 1], str(self.images))
        self.assertNotIn("--tests", args)                   # every test ticked = the exerciser default
        self.assertEqual(args[args.index("--expect-build") + 1], "abc1234")
        self.assertFalse(diag.marker_file().exists())
        self.assertFalse(diag.current_file().exists())

        page = self.client.get(f"/diag/result/{job}").get_data(as_text=True)
        self.assertIn("Capture complete - download the zip and send it to us", page)
        self.assertIn("Restored: OK", page)
        name = f"tf4060-diag-00000000feedf00d-{time.strftime('%Y%m%d')}.zip"
        self.assertIn(name, page)
        for member in ("before.json", "report.html", "board/board.json", "restore.json",
                       "progress.jsonl", f"logs/{job}.log", "contact.txt"):
            self.assertIn(f'<td class="mono">{member}</td>', page)
        self.assertIn("power-cycle the Amiga before you use it", page)
        self.assertIn('value="amigafan"', page)

        download = self.client.get(f"/diag/result/{job}/download")
        self.assertEqual(download.status_code, 200)
        self.assertIn(f"attachment; filename={name}", download.headers["Content-Disposition"])
        with zipfile.ZipFile(io.BytesIO(download.data)) as archive:
            report = archive.read("report.html").decode()
            contact = archive.read("contact.txt").decode()
        download.close()
        self.assertNotIn("192.168.1.20", report)
        self.assertNotIn("fe80::1", report)
        self.assertNotIn("stub-pi.local", report)
        self.assertIn("12:34:56", report)                  # times are not addresses
        self.assertIn("Discord / forum handle: amigafan\n", contact)
        self.assertIn("Hangs at 75 MHz\nafter[0m boot", contact)
        self.assertNotIn("\x1b", contact)
        self.assertNotIn("\x07", contact)

    def test_contact_added_after_the_run(self):
        job = self.start("board").headers["Location"].rsplit("/", 1)[1]
        self.wait(job)
        page = self.client.get(f"/diag/result/{job}").get_data(as_text=True)
        self.assertNotIn('<td class="mono">contact.txt</td>', page)
        self.client.post(f"/diag/result/{job}/contact", data={"csrf": self.csrf(), "handle": "x" * 100,
                                                             "note": "n" * 3000})
        page = self.client.get(f"/diag/result/{job}").get_data(as_text=True)
        self.assertIn('<td class="mono">contact.txt</td>', page)   # shown in the contents list
        found = diag.find_zip(diag.runs_dir() / job)
        with zipfile.ZipFile(found) as archive:
            text = archive.read("contact.txt").decode()
        self.assertIn("x" * diag.HANDLE_MAX + "\n", text)
        self.assertNotIn("x" * (diag.HANDLE_MAX + 1), text)
        self.assertIn("n" * diag.NOTE_MAX, text)
        self.assertNotIn("n" * (diag.NOTE_MAX + 1), text)
        self.client.post(f"/diag/result/{job}/contact", data={"csrf": self.csrf(), "handle": "", "note": ""})
        self.assertNotIn('<td class="mono">contact.txt</td>',
                         self.client.get(f"/diag/result/{job}").get_data(as_text=True))

    def test_findings_are_observations_not_failures(self):
        os.environ["FAKE_DIAG_FINDING"] = "board"
        job = self.start("board").headers["Location"].rsplit("/", 1)[1]
        done = self.wait(job)
        self.assertTrue(done["result"]["ok"])                 # capture complete, restored
        self.assertIn("--no-timing", self.diag_log.read_text())
        self.assertEqual(done["result"]["steps"], {"board": {"state": "captured", "findings": 1}})
        self.assertIn("FINDING net_TS: shorted to SIZ60_0", done["text"])
        page = self.client.get(f"/diag/result/{job}").get_data(as_text=True)
        self.assertIn("1 finding noted", page)
        self.assertIn("Capture complete - download the zip and send it to us", page)

    def test_incomplete_capture(self):
        os.environ["FAKE_DIAG_INCOMPLETE"] = "timing"
        job = self.start("timing").headers["Location"].rsplit("/", 1)[1]
        done = self.wait(job)
        self.assertFalse(done["result"]["complete"])
        self.assertTrue(done["result"]["restored"])
        self.assertIn("Capture incomplete - download the zip", done["result"]["result"])
        self.assertIn("Capture incomplete", self.client.get(f"/diag/result/{job}").get_data(as_text=True))

    def test_restore_failure_is_red_with_advice(self):
        os.environ["FAKE_DIAG_RESTORE_BAD"] = "1"
        job = self.start("timing").headers["Location"].rsplit("/", 1)[1]
        done = self.wait(job)
        self.assertFalse(done["result"]["restored"])
        page = self.client.get(f"/diag/result/{job}").get_data(as_text=True)
        self.assertIn("Restored: NOT OK", page)
        self.assertIn("CPLD usercode differs", page)
        self.assertIn("reflash the firmware from the main TF Pi page", page)
        self.assertIn('class="banner-bad"', page)
        # tfdiag kept its marker: the tab asks for a restore check before more tests.
        self.assertTrue(diag.marker_file().exists())
        tab = self.client.get("/diag").get_data(as_text=True)
        self.assertIn("Run the restore check", tab)
        self.assertIn("/diag", self.start().headers["Location"])
        self.assertEqual(self.calls().count("run"), 1)

    def test_abort_still_restores(self):
        os.environ["FAKE_DIAG_HANG"] = "board"
        job = self.start("all").headers["Location"].rsplit("/", 1)[1]
        self.wait_running()
        self.client.post(f"/diag/abort/{job}", data={"csrf": self.csrf()})
        done = self.wait(job)
        self.assertTrue(done["result"]["aborted"])
        self.assertTrue(done["result"]["restored"])
        self.assertIn("interrupted (SIGTERM): restoring", done["text"])
        self.assertIn("Capture stopped early", done["result"]["result"])
        self.assertFalse((diag.runs_dir() / job / "timing").exists())     # timing never ran


class ReportTests(DiagWebBase):
    def run_timing(self):
        job = self.start("timing").headers["Location"].rsplit("/", 1)[1]
        self.wait(job)
        return job, diag.runs_dir() / job

    def test_report_served_sandboxed_and_shown(self):
        job, rundir = self.run_timing()
        page = self.client.get(f"/diag/result/{job}").get_data(as_text=True)
        self.assertIn(f'<iframe src="/diag/result/{job}/file/report.html" sandbox="allow-scripts"', page)
        self.assertIn("Open the report in a new tab", page)
        reply = self.client.get(f"/diag/result/{job}/file/report.html")
        self.assertEqual(reply.status_code, 200)
        self.assertEqual(reply.mimetype, "text/html")
        self.assertIn("<script>window.boardview = 1</script>", reply.get_data(as_text=True))   # served as-is
        csp = reply.headers["Content-Security-Policy"]
        self.assertIn("sandbox allow-scripts", csp)
        self.assertIn("script-src 'unsafe-inline'", csp)
        self.assertNotIn("allow-same-origin", csp)
        reply.close()

    def test_waveform_svgs_listed(self):
        job, rundir = self.run_timing()
        (rundir / "timing" / "waves").mkdir(parents=True, exist_ok=True)
        (rundir / "timing" / "waves" / "wave_p0_k1_r0.svg").write_text("<svg xmlns='http://www.w3.org/2000/svg'/>")
        page = self.client.get(f"/diag/result/{job}").get_data(as_text=True)
        self.assertIn(f'href="/diag/result/{job}/file/timing/waves/wave_p0_k1_r0.svg"', page)
        reply = self.client.get(f"/diag/result/{job}/file/timing/waves/wave_p0_k1_r0.svg")
        self.assertEqual(reply.mimetype, "image/svg+xml")
        reply.close()

    def test_render_waves_survives_a_bad_vcd(self):
        job, rundir = self.run_timing()
        (rundir / "timing" / "waves").mkdir(parents=True, exist_ok=True)
        (rundir / "timing" / "waves" / "junk.vcd").write_text("not a vcd")
        diag.render_waves(rundir)                               # logs, doesn't raise

    def test_only_files_inside_the_run(self):
        job, rundir = self.run_timing()
        other, _ = self.run_timing()
        config.write_json(config.WEB_CONF.parent / "secret.html", {"x": 1})
        (rundir.parent / "outside.html").write_text("no")
        for name in ("../outside.html", "..%2Foutside.html", "../../../../etc/web.json",
                     f"../{other}/report.html", "/etc/passwd", "before.json", "restore.json",
                     "progress.jsonl", "nothere.html", "timing", "..\\outside.html"):
            reply = self.client.get(f"/diag/result/{job}/file/{name}", follow_redirects=True)
            self.assertEqual(reply.status_code, 404, name)      # "//etc" is a 308 to a 404 first
            reply.close()
        self.assertIsNone(diag.viewable(rundir, "../outside.html"))
        self.assertIsNone(diag.viewable(rundir, str(rundir / "report.html")))   # absolute
        (rundir / "link.html").symlink_to(rundir.parent / "outside.html")
        self.assertIsNone(diag.viewable(rundir, "link.html"))                  # symlink out

    def test_needs_login(self):
        job, _ = self.run_timing()
        self.client.post("/logout", data={"csrf": self.csrf()})
        reply = self.client.get(f"/diag/result/{job}/file/report.html")
        self.assertEqual(reply.status_code, 302)
        self.assertIn("/login", reply.headers["Location"])


def hist_entry(meas_bins, shift=2, cycles=1000):
    """One region's 0xA8 record: 8 measurements x 32 bins (only the given ones filled)."""
    bins = [[0] * 32 for _ in range(8)]
    for m, filled in meas_bins.items():
        for b, c in filled.items():
            bins[m][b] = c
    return {"bins": bins, "shift": shift, "as": cycles, "windows": 4, "seconds": 2.0, "lost": 0}


class ResultSummaryTests(DiagWebBase):
    """The result page's own bus-cycle table, waveforms, findings and clocks."""

    def fixture_run(self, job="diag-20261106-120000-aaaaaa"):
        rundir = diag.runs_dir() / job
        waves = rundir / "timing" / "waves"
        waves.mkdir(parents=True)
        (rundir / "board").mkdir()
        hist = {"0": {"chip": hist_entry({0: {9: 600, 10: 400}, 1: {11: 300}}),
                      "cia": hist_entry({0: {30: 50}}, shift=5, cycles=50)},
                "2": {"chip": hist_entry({0: {8: 900}}),
                      "ramsey": hist_entry({2: {3: 700}}, cycles=700)}}
        (rundir / "timing" / "histograms.json").write_text(json.dumps(
            {"measurements": [], "fabric_mhz": {"0": 100.02, "2": 124.9}, "histograms": hist}))
        timing = {"fabric_mhz": {"0": 100.02, "2": 124.9},
                  "clocks": {"clocks": [{"name": "fabric", "mhz": 100.0, "nominal": 100.0, "status": "info",
                                         "msg": "FPGA PLL clock 100.00 MHz by Pi time"},
                                        {"name": "CLKAUX", "mhz": 25.0, "nominal": 25.0, "status": "info", "msg": ""}]},
                  "clock_switches": [{"preset": 2, "acked": True, "polls": 1520, "bus_errors": 2, "settled": True}],
                  "waves": [{"preset": 2, "kind": 3, "region": 1, "file": "waves/wave_p2_k3_r1.vcd", "trigger": "AS20 fall in region"},
                            {"preset": 0, "kind": 3, "region": 1, "file": "waves/wave_p0_k3_r1.vcd", "trigger": "AS20 fall in region"},
                            {"preset": 2, "kind": 4, "region": 0, "file": "waves/wave_p2_k4_r0.vcd", "trigger": "STERM"},
                            {"preset": 0, "kind": 3, "region": 2, "file": "waves/wave_p0_k3_r2.vcd", "trigger": "x"}]}
        (rundir / "timing" / "timing.json").write_text(json.dumps(timing))
        for name in ("wave_p0_k3_r1", "wave_p2_k3_r1", "wave_p2_k4_r0"):          # r2 has no SVG
            (waves / f"{name}.svg").write_text("<svg xmlns='http://www.w3.org/2000/svg'/>")
        (rundir / "board" / "board.json").write_text("{}")
        config.write_json(rundir / "outcome.json", {"ok": True, "complete": True, "restored": True,
                                                    "steps": {"board": {"state": "captured", "findings": 0},
                                                              "timing": {"state": "captured", "findings": 0}}})
        return job, rundir

    def test_bus_cycle_table_matches_tfdiag(self):
        from tfdiag import amiga
        job, rundir = self.fixture_run()
        bus = diag.bus_cycles(rundir, diag.images_status()["info"])
        self.assertEqual([c["preset"] for c in bus["columns"]], [0, 2])
        self.assertEqual(bus["columns"][1]["fabric_mhz"], 124.9)
        rows = {r["region"]: r for r in bus["rows"]}
        self.assertEqual([r["region"] for r in bus["rows"]], amiga.REGION_ORDER)
        chip0 = rows["chip"]["cells"][0]
        want = amiga.hist_summary(0, hist_entry({0: {9: 600, 10: 400}})["bins"][0], 2, 100.02)
        self.assertEqual((chip0["meas"][0]["median"], chip0["meas"][0]["min"], chip0["meas"][0]["n"]),
                         (want["median"], want["min"], 1000))                      # same numbers as the report
        self.assertIsNotNone(chip0["meas"][1])                                     # writes
        self.assertIsNone(chip0["meas"][2])                                        # no STERM on chip RAM
        self.assertFalse(rows["ramsey"]["cells"][0]["seen"])                       # p0: no motherboard RAM cycles
        self.assertTrue(rows["ramsey"]["cells"][1]["seen"])
        self.assertFalse(rows["rom"]["cells"][0]["seen"])

        page = self.client.get(f"/diag/result/{job}").get_data(as_text=True)
        self.assertIn("<h2>Bus cycles</h2>", page)
        self.assertIn("p2 · 62.5 MHz (half bus)", page)
        self.assertIn("fabric 124.90 MHz", page)
        self.assertIn(f"<b>{want['median']} ns</b>", page)
        self.assertIn("not seen", page)
        self.assertIn("1,000 cycles", page)
        self.assertNotIn('class="bad"', page.split("<h2>Bus cycles</h2>")[1].split("</table>")[0])
        self.assertLess(page.index("<h2>Bus cycles</h2>"), page.index("Back to Diagnostics"))
        (rundir / "report.html").write_text("<p>report</p>")
        diag.rebuild_zip(rundir)
        page = self.client.get(f"/diag/result/{job}").get_data(as_text=True)
        order = [page.index(h) for h in ("<h2>Bus cycles</h2>", "<h2>Waveforms per region</h2>",
                                         "<h2>Clocks</h2>", "<h2>Report</h2>", "<h2>Download</h2>")]
        self.assertEqual(order, sorted(order))              # summary above the report and the download

    def test_waveforms_per_region_with_preset_selector(self):
        job, rundir = self.fixture_run()
        waves = diag.waves_by_region(rundir)
        self.assertEqual([c["preset"] for c in waves["chip"]], [0, 2])            # preset 0 first
        self.assertIn("ramsey", waves)                                             # STERM capture
        self.assertNotIn("cia", waves)                                             # no SVG for it
        page = self.client.get(f"/diag/result/{job}").get_data(as_text=True)
        self.assertIn(f'<img id="wave-1" src="/diag/result/{job}/file/timing/waves/wave_p0_k3_r1.svg"', page)
        self.assertIn(f'<option value="/diag/result/{job}/file/timing/waves/wave_p2_k3_r1.svg">p2', page)
        self.assertIn(f'href="/diag/result/{job}/file/timing/waves/wave_p2_k4_r0.svg"', page)   # full list
        svg = self.client.get(f"/diag/result/{job}/file/timing/waves/wave_p0_k3_r1.svg")
        self.assertEqual(svg.mimetype, "image/svg+xml")
        svg.close()

    def test_clocks_and_switches(self):
        job, rundir = self.fixture_run()
        page = self.client.get(f"/diag/result/{job}").get_data(as_text=True)
        self.assertIn("<h2>Clocks</h2>", page)
        self.assertIn("100.0 MHz", page)
        self.assertIn("p2 124.90 MHz", page)
        self.assertIn("<td>p2</td><td>1520</td><td>2</td><td>yes</td>", page)

    def test_board_findings_summary(self):
        job, rundir = self.fixture_run()
        (rundir / "board" / "board.json").write_text(json.dumps({"chain": {"idcode_ok": True}, "extest": {
            "nets": [{"name": "TS", "net": "TS", "cpld_pin": 12, "status": "finding",
                      "msg": "shorted to SIZ60_0 (CPLD pin 13)"}],
            "findings": [{"kind": "short", "a": "TS", "b": "SIZ60_0", "adjacent": []}]}}))
        fs = diag.board_findings(rundir)
        self.assertGreaterEqual(fs["count"], 1)
        page = self.client.get(f"/diag/result/{job}").get_data(as_text=True)
        self.assertIn("<h2>Board test findings (experimental)</h2>", page)
        self.assertIn("should not be taken as an indication of a real fault", page)
        self.assertIn(fs["first"][0]["title"], page)

    def test_exerciser_findings_get_their_own_card(self):
        job, rundir = self.fixture_run()
        tm = json.loads((rundir / "timing" / "timing.json").read_text())
        tm["results"] = [{"preset": 0, "test": 0x53, "group": "test 0x53", "size": 4, "flags": [], "failure": True,
                          "addr": "0x00bfe400", "expected": "0x00a500a5", "got": "0x00a500ff"}]
        tm["hangs"] = [{"preset": 2, "group": "clock switch", "why": "stopped (heartbeat frozen)"}]
        (rundir / "timing" / "timing.json").write_text(json.dumps(tm))
        fs = diag.board_findings(rundir)
        self.assertEqual(fs["exerciser"]["count"], 2)
        self.assertTrue(all("0x53" not in f["title"] for f in fs["first"]))
        page = self.client.get(f"/diag/result/{job}").get_data(as_text=True)
        self.assertIn("<h2>Timing capture findings</h2>", page)
        board_card = page.split("<h2>Board test findings (experimental)</h2>")[-1].split("</div>")[0] \
            if "Board test findings" in page else ""
        self.assertNotIn("test 0x53", board_card)

    def test_partial_or_broken_files_still_render(self):
        job, rundir = self.fixture_run()
        (rundir / "timing" / "histograms.json").write_text('{"histograms": {"0": {"chip": {"bins": "nonsense"}}}}')
        (rundir / "timing" / "timing.json").write_text("not json")
        reply = self.client.get(f"/diag/result/{job}")
        self.assertEqual(reply.status_code, 200)
        self.assertIn("Couldn't read the timing results", reply.get_data(as_text=True))

    def test_real_tfdiag_run_renders(self):
        # A whole capture from tfdiag's fake board: the page reads tfdiag's real output.
        from tfdiag.tests import helpers as th
        env = th.Env()
        self.addCleanup(env.cleanup)
        out = env.tmp / "job"
        rc, evs = th.run_cli(env, ["run", "--images", str(env.images), "--out", str(out),
                                   "--presets", "0,2", "--secs", "0.2"])
        self.assertIn(rc, (0, 1), evs[-1])
        job = "diag-20261106-130000-bbbbbb"
        import shutil
        shutil.copytree(out, diag.runs_dir() / job)
        rundir = diag.runs_dir() / job
        config.write_json(rundir / "outcome.json", {"ok": True, "complete": True, "restored": True,
                                                    "steps": {"board": {"state": "captured", "findings": 0}}})
        diag.render_waves(rundir)
        summary = diag.page_summary(rundir)
        self.assertIsNone(summary.get("bus_error"), summary)
        self.assertTrue(summary["bus"]["rows"])
        self.assertTrue(any(c["seen"] for r in summary["bus"]["rows"] for c in r["cells"]))
        page = self.client.get(f"/diag/result/{job}").get_data(as_text=True)
        self.assertIn("<h2>Bus cycles</h2>", page)
        self.assertIn("Chip RAM", page)
        if summary["waves"]:
            self.assertIn("<h2>Waveforms per region</h2>", page)
        self.assertIn("<h2>Clocks</h2>", page)
        self.assertIsNone(summary.get("firmware_error"), summary)
        self.assertIn("<h2>Firmware on the board</h2>", page)
        self.assertIn("fuses ", summary["firmware"]["cpld"])
        self.assertIn("bitstream ", summary["firmware"]["fpga"])
        self.assertTrue((rundir / "firmware" / "cpld_readback.jed").exists())

    def test_expansion_boards_card(self):
        job, rundir = self.fixture_run()
        tm = json.loads((rundir / "timing" / "timing.json").read_text())
        tm["expansion_boards"] = {"boards": [{"index": 0, "zorro": "III", "manufacturer": 28014,
                                              "manufacturer_name": "MNT", "product": 4, "serial": "0x00000001",
                                              "er_type": "0x82", "er_flags": "0x20", "size": 64 << 20,
                                              "memory": False, "boot_rom": False, "chained": False,
                                              "no_shutup": False, "diag_vec": "0x0000", "configured_at": None}],
                                  "end": {"count": 1, "reason": 0, "reason_text": "no more boards",
                                          "z3_bus_errors": 0, "z2_bus_errors": 0}}
        (rundir / "timing" / "timing.json").write_text(json.dumps(tm))
        page = self.client.get(f"/diag/result/{job}").get_data(as_text=True)
        self.assertIn("<h2>Expansion boards</h2>", page)
        self.assertIn("Zorro III I/O, 64 MB: MNT 28014", page)
        self.assertIn("no more boards", page)

    def test_old_runs_without_firmware_ids_render(self):
        job, rundir = self.fixture_run()
        self.assertIsNone(diag.firmware_on_board(rundir))
        page = self.client.get(f"/diag/result/{job}").get_data(as_text=True)
        self.assertNotIn("Firmware on the board", page)


class LockoutTests(DiagWebBase):
    def review_token(self):
        data = make_zip(self.tmp / "new.zip", {"tf4060r5_ram.bin": full_image(9), "tf4060r5_bus_top.jed": jed()}).read_bytes()
        reply = self.client.post("/upload", data={"csrf": self.csrf(), "firmware": (io.BytesIO(data), "new.zip")},
                                 content_type="multipart/form-data")
        return reply.headers["Location"].rsplit("/", 1)[1]

    def test_no_flash_or_second_diag_while_diag_runs(self):
        token = self.review_token()
        os.environ["FAKE_DIAG_HANG"] = "board"
        job = self.start("board").headers["Location"].rsplit("/", 1)[1]
        self.wait_running()

        refused = self.client.post(f"/flash/{token}", data={"csrf": self.csrf()}, follow_redirects=True)
        self.assertIn("diagnostic test", refused.get_data(as_text=True))
        self.assertIn("A diagnostic test or its restore is running", self.client.get(f"/review/{token}")
                      .get_data(as_text=True))
        second = self.start("timing")
        self.assertIn("/diag", second.headers["Location"])
        self.assertEqual(len(list(config.JOB_DIR.glob("diag-*.log"))), 1)
        update = self.client.post("/update/start", data={"csrf": self.csrf()}, follow_redirects=True)
        self.assertIn("diagnostic test", update.get_data(as_text=True))
        self.assertEqual(flasher.main(["flash", str(self.tmp / "new.zip")]), 1)   # CLI too
        self.assertIn("A diagnostic test is running", self.client.get("/").get_data(as_text=True))

        self.client.post(f"/diag/abort/{job}", data={"csrf": self.csrf()})
        self.wait(job)
        self.assertEqual(config.load_state()["fpga"]["flashes"], 0)
        ok = self.client.post(f"/flash/{token}", data={"csrf": self.csrf()})
        self.assertIn("/job/", ok.headers["Location"])          # free again

    def test_no_diag_while_flash_runs(self):
        config.STATE_DIR.mkdir(parents=True, exist_ok=True)
        with open(config.LOCK_FILE, "w") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            reply = self.start("all")
            self.assertIn("/diag", reply.headers["Location"])
            page = self.client.get("/diag").get_data(as_text=True)
            self.assertIn("already running", page)
        self.assertEqual(self.calls(), [])

    def test_job_refuses_if_flash_lock_taken(self):
        # The web check can race a flash starting; the job's own lock check is the guard.
        config.STATE_DIR.mkdir(parents=True, exist_ok=True)
        with open(config.LOCK_FILE, "w") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            self.assertEqual(diag.main(["run", "--job", "diag-x", "--result", str(self.tmp / "r.json")]), 2)
        self.assertIn("running", json.loads((self.tmp / "r.json").read_text())["result"])
        self.assertEqual(self.calls(), [])


class RecoveryTests(DiagWebBase):
    def interrupted_run(self, job="diag-20261106-101010-abcdef"):
        """What a power cut during the timing step leaves behind."""
        rundir = diag.runs_dir() / job
        (rundir / "board").mkdir(parents=True)
        (rundir / "board" / "board.json").write_text("{}")
        (rundir / "before.json").write_text(json.dumps({"serial": "00000000feedf00d"}))
        config.write_json(rundir / "run.json", {"job": job, "date": "20261106", "started": "x"})
        config.write_json(diag.current_file(), {"job": job, "rundir": str(rundir), "pid": 999999})
        diag.marker_file().write_text(json.dumps({"before": {"serial": "00000000feedf00d"},
                                                  "before_file": str(rundir / "before.json")}))
        config.JOB_DIR.mkdir(parents=True, exist_ok=True)
        (config.JOB_DIR / f"{job}.log").write_text("[10:10:10] --- Board test\n")
        return job, rundir

    def test_power_loss_recovered_ok(self):
        job, rundir = self.interrupted_run()
        self.assertTrue(diag.pending_restore())
        self.assertEqual(diag.main(["recover"]), 0)
        self.assertEqual(self.calls(), ["recover"])
        self.assertIn(str(rundir / "before.json"), self.diag_log.read_text())
        self.assertFalse(diag.marker_file().exists())
        self.assertFalse(diag.pending_restore())
        for page in ("/diag", "/"):
            self.assertIn("A diagnostic test was interrupted by power loss: recovered OK",
                          self.client.get(page).get_data(as_text=True))
        result = self.client.get(f"/diag/result/{job}").get_data(as_text=True)
        self.assertIn("interrupted", result)
        self.assertIn("board/board.json", result)
        self.assertIn(f"logs/{job}.log", result)
        download = self.client.get(f"/diag/result/{job}/download")
        self.assertEqual(download.status_code, 200)
        download.close()
        self.assertFalse(config.read_json(config.JOB_DIR / f"{job}.json", {})["ok"])
        self.client.post("/diag/recovery/dismiss", data={"csrf": self.csrf()})
        self.assertNotIn("interrupted by power loss", self.client.get("/diag").get_data(as_text=True))

    def test_power_loss_recovery_failed(self):
        self.interrupted_run()
        os.environ["FAKE_DIAG_RESTORE_BAD"] = "1"
        self.assertEqual(diag.main(["recover"]), 1)
        page = self.client.get("/diag").get_data(as_text=True)
        self.assertIn("interrupted by power loss and the machine was not restored", page)
        self.assertIn("CPLD usercode differs", page)
        self.assertIn("Power-cycle the Amiga", page)
        self.assertIn("Run the restore check", page)          # marker kept: offer it again

    def test_restore_check_from_page_then_forget(self):
        self.interrupted_run()
        os.environ["FAKE_DIAG_RESTORE_BAD"] = "1"
        reply = self.client.post("/diag/recover", data={"csrf": self.csrf()})
        job = reply.headers["Location"].rsplit("/", 1)[1]
        self.assertTrue(job.startswith("diagrecover-"))
        done = self.wait(job)
        self.assertFalse(done["result"]["restored"])
        self.assertTrue(diag.pending_restore())
        self.client.post("/diag/forget", data={"csrf": self.csrf()})
        self.assertFalse(diag.pending_restore())

    def test_interrupted_before_hardware(self):
        self.interrupted_run()
        diag.marker_file().unlink()                           # cut before tfdiag's snapshot
        self.assertEqual(diag.main(["recover"]), 0)
        self.assertEqual(self.calls(), [])                    # nothing to restore
        self.assertTrue(diag.recovery()["ok"])

    def test_nothing_to_recover(self):
        self.assertEqual(diag.main(["recover"]), 0)
        self.assertIsNone(diag.recovery())


class UnitAndInstallTests(DiagBase):
    def test_recovery_unit(self):
        unit = configparser.ConfigParser(strict=False, interpolation=None)
        unit.optionxform = str
        text = (ROOT / "systemd" / "tfpi-diag-recover.service").read_text()
        conditions = re.findall(r"^ConditionPathExists=\|?(.+)$", text, re.M)
        unit.read_string(text)
        self.assertEqual(unit["Service"]["Type"], "oneshot")
        self.assertIn("tfpi.diag recover", unit["Service"]["ExecStart"])
        self.assertEqual(unit["Service"]["Environment"], "PYTHONPATH=/opt/tfpi/lib")   # tfpi and tfdiag
        before = unit["Unit"]["Before"].split()
        self.assertIn("a314d.service", before)
        self.assertIn("tfpi-web.service", before)
        self.assertNotIn("Requires", unit["Unit"])            # a314d starts whatever happens
        # The conditions are the files the code uses on a real Pi.
        config.set_paths("/etc/tfpi", "/var/lib/tfpi", "/opt/tfpi")
        self.assertEqual(sorted(conditions), sorted([str(diag.marker_file()), str(diag.current_file())]))

    def test_install_sh(self):
        text = (ROOT / "install.sh").read_text()
        self.assertIn("openocd", text)
        self.assertIn("python3-spidev", text)
        self.assertIn('cp -r "$self_dir/tfdiag" "$PREFIX/lib/tfdiag"', text)
        self.assertIn("systemd/tfpi-diag-recover.service", text)
        self.assertIn("systemctl enable tfpi-diag-recover.service", text)

    def test_images_dir_ships_with_tfpi(self):
        self.assertTrue((ROOT / "tfpi" / "diag_images").is_dir())
        os.environ.pop("TFPI_DIAG_IMAGES")
        self.assertEqual(diag.images_dir(), ROOT / "tfpi" / "diag_images")

    def test_render(self):
        self.assertIn("40% exercising", diag.render('{"event":"progress","pct":40,"msg":"exercising"}'))
        self.assertIn("FINDING net_SD7: stuck low", diag.render(
            '{"event":"result","step":"extest","name":"net_SD7","status":"finding","msg":"stuck low"}'))
        self.assertEqual(diag.render('{"event":"result","step":"timing","name":"clock_fabric",'
                                     '"status":"info","msg":"100 MHz"}'), "    clock_fabric: 100 MHz")
        self.assertIn("WARN: hot", diag.render('{"event":"log","level":"warn","msg":"hot"}'))
        self.assertEqual(diag.render("Info : JTAG tap found\n"), "    Info : JTAG tap found")

    def test_clean_text(self):
        self.assertEqual(diag.clean_text(" a\x00b\x9bc\td ", 64), "abcd")
        self.assertEqual(diag.clean_text("l1\r\nl2\x1b", 64, lines=True), "l1\nl2")
