"""JSON-lines events on stdout (the web tab shows them as a live log).

  {"event":"step","step":"jtag","msg":...}
  {"event":"progress","pct":40,"msg":...}
  {"event":"log","level":"info|warn|error","msg":...}
  {"event":"result","step":...,"name":...,"status":"info|finding|incomplete|pass|fail","msg":...}
     info: a measurement or an ID; finding: something the board test or the
     exerciser saw (open, short, stuck pin, missing clock, chain problem, wrong
     data, hang), worded as an observation: good/bad is decided at home base;
     incomplete: part of the capture could not be done; pass/fail: the restore
     check only (a real check: is the machine back as it was?).
  {"event":"done","ok":bool,"summary":...}
Every event also carries "t" (seconds since the epoch). A copy of every event
goes to the optional log file (bundled).
"""

import json
import sys
import time


class Events:
    def __init__(self, stream=None, logfile=None):
        self.stream = stream if stream is not None else sys.stdout
        self.logfile = logfile
        self.results = []
        self.step_name = None

    def emit(self, event, **kw):
        rec = {"event": event, "t": round(time.time(), 3)}
        rec.update(kw)
        line = json.dumps(rec, default=str)
        try:
            self.stream.write(line + "\n")
            self.stream.flush()
        except (OSError, ValueError):
            pass
        if self.logfile:
            try:
                with open(self.logfile, "a") as fh:
                    fh.write(line + "\n")
            except OSError:
                pass
        return rec

    def step(self, step, msg=""):
        self.step_name = step
        return self.emit("step", step=step, msg=msg)

    def progress(self, pct, msg=""):
        return self.emit("progress", pct=int(max(0, min(100, pct))), msg=msg)

    def log(self, msg, level="info"):
        return self.emit("log", level=level, msg=msg)

    def info(self, msg):
        return self.log(msg, "info")

    def warn(self, msg):
        return self.log(msg, "warn")

    def error(self, msg):
        return self.log(msg, "error")

    def result(self, name, status, msg="", step=None, **kw):
        rec = self.emit("result", step=step or self.step_name, name=name, status=status, msg=msg, **kw)
        self.results.append(rec)
        return rec

    def done(self, ok, summary, **kw):
        return self.emit("done", ok=bool(ok), summary=summary, **kw)

    def incomplete(self):
        """Part of the capture could not be done."""
        return any(r["status"] == "incomplete" for r in self.results)

    def findings(self):
        return sum(1 for r in self.results if r["status"] == "finding")


class Quiet(Events):
    """Events that go nowhere (library use, tests)."""

    def __init__(self):
        super().__init__(stream=_Null())


class _Null:
    def write(self, s):
        pass

    def flush(self):
        pass
