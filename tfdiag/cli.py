"""python3 -m tfdiag <command> - TF4060 diagnostics on the TF Pi.

JSON lines on stdout (see events.py). No pass/fail on the captured data: good or
bad is decided at home base from the zip. Exit status: 0 capture complete (and,
for run/restore/recover, the machine restored as before), 1 capture incomplete or
the restore check found a difference, 2 could not run (tool missing, lock held,
no snapshot, bad arguments ...).

  snapshot --out DIR/before.json
  board    [--images IMGDIR] --out DIR/board
  timing   [--images IMGDIR] --out DIR/timing [--presets 0,2,5] [--tests 0x77F] [--iters N]
           [--secs 0.5] [--expect-build abc1234]
  restore  [--before DIR/before.json] [--out DIR/restore.json]
  recover  [--before DIR/before.json] [--out DIR/restore.json]
  run      [--images IMGDIR] --out DIR [--no-board] [--no-timing] [timing options]
  report   --dir DIR [--machine A3000|A4000|A4000T]
  bundle   --dir DIR --out FILE.zip
IMGDIR (default: config `images`, /opt/tfpi/diag) holds diag_timing.bin, diag_pins.bin,
diagex_body.bin and diag_manifest.json.
"""

import argparse
import json
import sys
import traceback
from pathlib import Path

from . import __version__
from . import config as C
from . import events as E
from . import session as S
from . import tools


class Usage(Exception):
    pass


def _parse_presets(text, info):
    if text:
        return [int(x, 0) for x in text.split(",") if x.strip()]
    return list(info.get("presets") or [0])


def _need_marker(cfg, args):
    if getattr(args, "no_marker_check", False):
        return
    if S.read_json(cfg.marker) is None:
        raise Usage("no snapshot taken (marker missing): run `tfdiag snapshot` first")


def _a314_stopped(hw, ev):
    if hw.a314.active():
        ev.warn("a314d is running: stopping it (it shares spidev0.0)")
        hw.a314.stop(ev)


def cmd_snapshot(cfg, hw, ev, args):
    before = S.snapshot(cfg, hw, ev, out=args.out, images=args.images)
    ok = all(before.get(k) for k in ("flash_jedec", "flash_sha256", "cpld_idcode"))
    ev.done(ok, "baseline recorded" if ok else "baseline incomplete", before=before)
    return 0 if ok else 1


def cmd_board(cfg, hw, ev, args):
    from . import board, timing
    _need_marker(cfg, args)
    _a314_stopped(hw, ev)
    S.set_stage(cfg, "board")
    info = timing.load_info(args.images)
    with hw.cpld() as cpld, hw.spi() as spi:
        res = board.run_board(cpld, spi, hw.fpga, ev, args.out, args.images, info)
    short = ev.incomplete()
    ev.done(not short, ("board test incomplete" if short else "board test captured")
            + f", {ev.findings()} findings", out=str(args.out), chain=res.get("chain"),
            findings=ev.findings())
    return 1 if short else 0


def check_cpld(hw, ev, info):
    """The timing image only runs on CPLD designs listed in the manifest."""
    from . import manifest
    with hw.cpld() as c:
        idc, uc = c.ids()
    ok, msg = manifest.cpld_compatible(info, uc)
    ev.result("cpld_compatibility", "info" if ok else "incomplete", msg, step="timing")
    return ok


def cmd_timing(cfg, hw, ev, args):
    from . import timing
    _need_marker(cfg, args)
    _a314_stopped(hw, ev)
    S.set_stage(cfg, "timing")
    info = timing.load_info(args.images)
    if not check_cpld(hw, ev, info):
        ev.done(False, "timing test not run: CPLD design not supported by the timing image")
        return 2
    presets = _parse_presets(args.presets, info)
    mask = int(args.tests, 0) if args.tests else 0
    ev.step("timing", f"timing capture, presets {presets}")
    with hw.spi() as spi:
        tr = timing.TimingRun(spi, hw.fpga, ev, args.out, args.images, info, secs=args.secs,
                              clock=hw.clock, iters=args.iters, hold=not args.no_hold,
                              region_secs=args.region_secs)
        res = tr.run(presets, mask, args.expect_build)
    short = ev.incomplete() or not res.get("ok")
    ev.done(not short, "timing capture incomplete" if short else "timing capture complete", out=str(args.out),
            hangs=len(res.get("hangs", [])), results=len(res.get("results", [])), findings=ev.findings())
    return 1 if short else 0


def _before_arg(args):
    if getattr(args, "before", None):
        b = S.read_json(args.before)
        if b is None:
            raise Usage(f"cannot read {args.before}")
        return b
    return None


def cmd_restore(cfg, hw, ev, args):
    res = S.restore(cfg, hw, ev, before=_before_arg(args), out=args.out)
    ev.done(res["ok"], res["advice"], differences=res["differences"])
    return 0 if res["ok"] else 1


def cmd_recover(cfg, hw, ev, args):
    res = S.recover(cfg, hw, ev, before=_before_arg(args), out=args.out)
    ev.done(res["ok"], res["advice"], differences=res["differences"])
    return 0 if res["ok"] else 1


def cmd_run(cfg, hw, ev, args):
    """The whole job in one process: snapshot, board, timing, report, restore."""
    from . import board, report, timing
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    from .hooks import checkpoint as cp
    info = timing.load_info(args.images)
    if S.read_json(cfg.marker) is not None:
        raise S.MarkerExists("a previous diagnostic has not been restored yet (marker present): "
                             "run `tfdiag restore` or `tfdiag recover` first")
    restored = None
    try:
        with S.signals_raise():
            before = S.snapshot(cfg, hw, ev, out=out / "before.json", images=args.images)
            try:
                if not all(before.get(k) for k in ("flash_jedec", "flash_sha256", "cpld_idcode")):
                    raise RuntimeError("the baseline is incomplete (flash or CPLD unreadable): "
                                       "no diag image is loaded without one")
                if not args.no_board:
                    S.set_stage(cfg, "board")
                    with hw.cpld() as cpld, hw.spi() as spi:
                        board.run_board(cpld, spi, hw.fpga, ev, out / "board", args.images, info)
                if not args.no_timing and check_cpld(hw, ev, info):
                    S.set_stage(cfg, "timing")
                    with hw.spi() as spi:
                        tr = timing.TimingRun(spi, hw.fpga, ev, out / "timing", args.images, info,
                                              secs=args.secs, clock=hw.clock, iters=args.iters,
                                              hold=not args.no_hold, region_secs=args.region_secs)
                        tr.run(_parse_presets(args.presets, info),
                               int(args.tests, 0) if args.tests else 0, args.expect_build)
            except S.Interrupted:
                raise
            except Exception as e:
                if not hw.present():
                    ev.result("board_lost", "incomplete", "the board went away during the test (power off?)")
                else:
                    ev.result("test_error", "incomplete", f"test stopped: {e}")
                    ev.error(traceback.format_exc(limit=4))
    except S.Interrupted as e:
        ev.warn(f"interrupted ({e}): restoring")
        ev.result("interrupted", "incomplete", f"capture stopped early ({e})")
    finally:
        cp("run.before_restore")
        restored = S.restore(cfg, hw, ev, out=out / "restore.json")
        cp("run.restored")
    try:
        report.write(out, ev)
    except Exception as e:
        ev.warn(f"report: {e}")
    short = ev.incomplete()
    ev.done(not short and restored["ok"], restored["advice"] if not restored["ok"] else
            "capture incomplete" if short else "capture complete", restore_ok=restored["ok"],
            complete=not short, findings=ev.findings())
    return 1 if short or not restored["ok"] else 0


def cmd_report(cfg, hw, ev, args):
    from . import report
    r = report.write(Path(args.dir), ev, machine=args.machine)
    ev.done(True, f"report: {r['counts']}", files=r["files"])
    return 0


def cmd_vcd_svg(cfg, hw, ev, args):
    from . import vcdsvg
    files = vcdsvg.run(args)
    for f in files:
        ev.info(f"{f['file']}: {f['cycles']} cycles, {f['start_ns']:g}-{f['end_ns']:g} ns")
    ev.done(True, f"{len(files)} SVG file(s)", files=[f["file"] for f in files])
    return 0


def cmd_bundle(cfg, hw, ev, args):
    from . import bundle
    contents = bundle.make(Path(args.dir), Path(args.out))
    ev.done(True, f"{len(contents)} files", file=str(args.out), contents=contents)
    return 0


def build_parser():
    ap = argparse.ArgumentParser(prog="tfdiag", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", help="JSON config (default /etc/tfpi/diag.json)")
    ap.add_argument("--marker", help="marker file (default /var/lib/tfpi/diag-in-progress)")
    ap.add_argument("--log", help="append every event to this file too")
    ap.add_argument("--version", action="version", version=__version__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("snapshot")
    p.add_argument("--out", type=Path)
    p.add_argument("--images", type=Path, help="diag images (default: config images); diag_timing.bin "
                   "holds the Amiga in reset from here on")
    for name in ("board", "timing", "run"):
        p = sub.add_parser(name)
        p.add_argument("--images", type=Path, help="diag images + diag_manifest.json (default: config images)")
        p.add_argument("--out", required=True, type=Path)
        p.add_argument("--no-marker-check", action="store_true", help=argparse.SUPPRESS)
        if name in ("timing", "run"):
            p.add_argument("--presets")
            p.add_argument("--tests")
            p.add_argument("--expect-build")
            p.add_argument("--secs", type=float, default=0.5, help="tprobe window per measurement")
            p.add_argument("--iters", type=int, default=0, help="exerciser iterations per test (0 = its default)")
            p.add_argument("--no-hold", action="store_true", help="do not use the exerciser's hold mode")
            p.add_argument("--region-secs", type=float, default=3.0,
                           help="hold mode: histogram time per region and preset")
        if name == "run":
            p.add_argument("--no-board", action="store_true")
            p.add_argument("--no-timing", action="store_true")
    for name in ("restore", "recover"):
        p = sub.add_parser(name)
        p.add_argument("--before", type=Path)
        p.add_argument("--out", type=Path)
    p = sub.add_parser("report")
    p.add_argument("--dir", required=True, type=Path)
    p.add_argument("--machine", choices=["A3000", "A4000", "A4000T"],
                   help="machine type shown in the report (default: from the Kickstart ROM)")
    from . import vcdsvg
    vcdsvg.add_args(sub.add_parser("vcd-svg", aliases=["vcd-png"],
                                   help="waveform VCD -> SVG timing diagram (stdlib only)"))
    p = sub.add_parser("bundle")
    p.add_argument("--dir", required=True, type=Path)
    p.add_argument("--out", required=True, type=Path)
    return ap


COMMANDS = {"snapshot": cmd_snapshot, "board": cmd_board, "timing": cmd_timing, "restore": cmd_restore,
            "recover": cmd_recover, "run": cmd_run, "report": cmd_report, "bundle": cmd_bundle, "vcd-svg": cmd_vcd_svg, "vcd-png": cmd_vcd_svg}
NO_HARDWARE = {"report", "bundle", "vcd-svg", "vcd-png"}


def main(argv=None, hw_factory=None, stream=None, cfg=None):
    ap = build_parser()
    args = ap.parse_args(argv)
    cfg = cfg or C.load(args.config)
    if args.marker:
        cfg.marker = args.marker
    ev = E.Events(stream=stream, logfile=args.log)
    hw = (hw_factory or S.Hw)(cfg, ev)
    if getattr(args, "images", "unset") is None:
        args.images = Path(cfg.images)
    try:
        if args.cmd in NO_HARDWARE:
            return COMMANDS[args.cmd](cfg, hw, ev, args)
        with S.locks(cfg):
            if args.cmd == "run":
                return cmd_run(cfg, hw, ev, args)
            with S.signals_raise():
                return COMMANDS[args.cmd](cfg, hw, ev, args)
    except (Usage, S.LockError, S.MarkerExists) as e:
        ev.error(str(e))
        ev.done(False, str(e))
        return 2
    except S.Interrupted as e:
        ev.error(f"interrupted ({e}); run `tfdiag restore` to bring the board back")
        ev.done(False, "interrupted")
        return 2
    except tools.ToolError as e:
        ev.error(str(e))
        ev.done(False, str(e))
        return 2
    except Exception as e:
        ev.error(f"internal error: {e!r}")
        ev.error(traceback.format_exc(limit=6))
        ev.done(False, f"internal error: {e!r}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
