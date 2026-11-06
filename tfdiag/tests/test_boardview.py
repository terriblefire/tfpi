"""Board view: data/r5_board.json (scripts/gen_r5_board.py) and findings -> board geometry."""

import html.parser
import json
import math
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

from tfdiag.tests import helpers  # noqa: F401  (sys.path)
from tfdiag import boardview as B
from tfdiag import netlist as NL
from tfdiag import report

MAJOR = ["BUSCPLD", "RAMFPGA", "IC1", "IC2", "IC3", "IC7", "IC8", "IC4", "IC5", "IC6", "IC9", "IC10",
         "OSC2", "CN100", "PI_HEADER", "JTAG1", "CPUJTAG"] + [f"U{i}" for i in range(1, 8)]


class _Html(html.parser.HTMLParser):
    VOID = {"meta", "br", "img", "input", "link", "path", "circle", "rect", "line"}

    def __init__(self):
        super().__init__()
        self.stack, self.bad = [], []

    def handle_starttag(self, tag, attrs):
        if tag not in self.VOID:
            self.stack.append(tag)

    def handle_startendtag(self, tag, attrs):
        pass                                        # <circle .../> in the SVG

    def handle_endtag(self, tag):
        if self.stack and self.stack[-1] == tag:
            self.stack.pop()
        else:
            self.bad.append(tag)


def parse_ok(test, text):
    p = _Html()
    p.feed(text)
    p.close()
    test.assertEqual((p.stack, p.bad), ([], []))
    s = text[text.index("<svg"):text.index("</svg>") + 6]
    return ET.fromstring(s)


class BoardJson(unittest.TestCase):
    def setUp(self):
        self.b = B.load_board()
        self.nl = NL.load()

    def test_shape(self):
        b = self.b
        for k in ("source", "outline", "bbox", "holes", "parts"):
            self.assertIn(k, b)
        x0, y0, x1, y1 = b["bbox"]
        self.assertGreater(x1 - x0, 150)
        self.assertGreater(y1 - y0, 80)
        self.assertGreaterEqual(len(b["outline"]), 4)
        for name in MAJOR:
            self.assertIn(name, b["parts"], name)
        for name, p in b["parts"].items():
            for k in ("value", "package", "x", "y", "rot", "mirror", "side", "body", "pads"):
                self.assertIn(k, p, name)
            self.assertIn(p["side"], ("top", "bottom"))
            self.assertEqual(p["side"] == "bottom", p["mirror"])
            self.assertEqual(len(p["body"]), 5)
            for pad in p["pads"]:
                self.assertEqual(len(pad), 6, (name, pad))
                self.assertIn(pad[4], ("t", "b", "th"))
                self.assertTrue(x0 - 3 <= pad[1] <= x1 + 3 and y0 - 3 <= pad[2] <= y1 + 3, (name, pad))
                if pad[4] != "th":
                    self.assertEqual(pad[4] == "b", p["mirror"], (name, pad))
        self.assertEqual(len(b["parts"]["BUSCPLD"]["pads"]), 144)
        self.assertEqual(b["parts"]["IC2"]["side"], "bottom")
        self.assertEqual(b["parts"]["IC8"]["side"], "top")

    def test_same_board_as_netlist(self):
        self.assertEqual(self.b["source"]["sha256"], self.nl.d["source"]["brd"]["sha256"])
        n = 0
        for name, p in self.nl.d["parts"].items():
            pads = {q[0]: q[5] for q in self.b["parts"][name]["pads"]}
            for pad, net in p["pads"].items():
                self.assertEqual(pads.get(pad), net, (name, pad))
                n += 1
        self.assertGreater(n, 1000)

    def test_neighbours_are_close(self):
        pads = {q[0]: q for q in self.b["parts"]["BUSCPLD"]["pads"]}
        d = math.dist(pads["2"][1:3], pads["3"][1:3])
        self.assertAlmostEqual(d, 0.5, delta=0.02)               # TQ144: 0.5 mm pitch
        for a, nbs in self.nl.d["neighbours"]["BUSCPLD"].items():
            for nb in nbs:
                self.assertLess(math.dist(pads[a][1:3], pads[nb][1:3]), 0.7, (a, nb))


class Findings(unittest.TestCase):
    def setUp(self):
        self.fs = B.findings(B.demo_results())
        self.by = {}
        for f in self.fs:
            self.by.setdefault(f["kind"], []).append(f)

    def test_demo_kinds(self):
        self.assertEqual(sorted(self.by), ["clock", "data lane", "hang", "open", "short", "stuck pin"])
        self.assertEqual(len(self.by["short"]), 1)                # DD4->DD5 and DD5->DD4 are one short
        for i, f in enumerate(self.fs):
            self.assertEqual(f["id"], f"f{i}")
            for k in ("kind", "title", "detail", "where", "nets", "pads", "parts", "pairs", "links", "colours"):
                self.assertIn(k, f)

    def test_short(self):
        f = self.by["short"][0]
        self.assertEqual(f["nets"], ["DD4", "DD5"])
        self.assertIn(["BUSCPLD", "6", "7"], f["pairs"])
        self.assertEqual(f["colours"], {"DD4": 0, "DD5": 1})
        self.assertIn("BUSCPLD", f["parts"])
        self.assertIn("BUSCPLD.6", f["where"])

    def test_open_stuck_clock(self):
        o = self.by["open"][0]
        self.assertEqual(o["nets"], ["TS"])
        self.assertEqual(o["pads"], [["BUSCPLD", "54"], ["RAMFPGA", "44"]])
        self.assertEqual(o["links"], [["BUSCPLD", "54", "RAMFPGA", "44"]])
        s = self.by["stuck pin"][0]
        self.assertEqual((s["nets"], s["pads"], s["pairs"]), (["DD15"], [["BUSCPLD", "19"]], [["BUSCPLD", "19", "18"]]))
        c = self.by["clock"][0]
        self.assertEqual(c["nets"], ["CLK25M"])
        self.assertIn(["OSC2", "3"], c["pads"])

    def test_lane(self):
        f = self.by["data lane"][0]
        self.assertEqual(f["nets"], ["DD11", "D11"])
        self.assertIn("(p2, p5) (2 results)", f["title"])
        self.assertEqual(f["colours"], {"DD11": 0, "D11": 0})
        self.assertEqual(self.by["hang"][0]["nets"], [])

    def test_data_lines(self):
        r = lambda size, addr, exp, got, flags=(): {"size": size, "addr": addr, "expected": exp, "got": got,
                                                     "flags": list(flags)}
        self.assertEqual(B.data_lines(r(4, "0x0", "0x00000000", "0x80000001")), [0, 31])
        self.assertEqual(B.data_lines(r(1, "0x00bfe001", "0x00000000", "0x00000001")), [16])   # byte 1: D23-16
        self.assertEqual(B.data_lines(r(1, "0x00000003", "0x00", "0x80")), [7])
        self.assertEqual(B.data_lines(r(2, "0x00000000", "0x0000", "0x0001")), [16])
        self.assertEqual(B.data_lines(r(2, "0x00000002", "0x0000", "0x8000")), [15])
        self.assertIsNone(B.data_lines(r(4, "0x0", "0x0", "0x1", ["exception"])))
        self.assertIsNone(B.data_lines({"size": 4}))

    def test_resolve(self):
        nl = NL.load()
        self.assertEqual(B.resolve_net(nl, "DD<3>"), "DD3")
        self.assertEqual(B.resolve_net(nl, "TS"), "TS")
        self.assertEqual(B.resolve_net(nl, "A[0]"), "AA0")          # FPGA PCF name
        self.assertIsNone(B.resolve_net(nl, "NOPE"))

    def test_robust(self):
        data = {"board": {"chain": {"idcode": "0x00000000", "idcode_ok": False, "ok": False,
                                    "error": "no CPLD on the JTAG chain"},
                          "cpu_in_reset": False,
                          "sample_normal": {"BCLK": {"pin": 30, "net": "BCLK", "state": "high", "samples": 16},
                                            "X": "junk"},
                          "extest": {"findings": [{"kind": "weird", "a": "NOPE"}, "junk", {"kind": "short", "a": None},
                                                  {"kind": "open", "a": "NOPE", "fpga_pin": None}],
                                     "nets": [{"name": "DD<1>", "net": "DD1", "cpld_pin": 3, "status": "finding",
                                               "msg": "something new"}],
                                     "float_changed": ["DD<2>"]}},
                "timing": {"results": [{"failure": True}, {"failure": True, "flags": ["summary"], "group": "chip RAM",
                                                           "expected": "0x10", "got": "0x2"}],
                           "clocks": {"clocks": [{"name": "NEWCLK", "status": "finding", "msg": "?"}]},
                           "hangs": ["junk", {"preset": 1}]}}
        fs = B.findings(data)
        kinds = [f["kind"] for f in fs]
        for k in ("JTAG chain", "reset", "clock", "weird", "net", "float", "exerciser", "hang"):
            self.assertIn(k, kinds)
        weird = next(f for f in fs if f["kind"] == "weird")
        self.assertEqual(weird["nets"], [])
        self.assertEqual(B.findings({"timing": {"clocks": "bad"}})[0]["kind"], "note")
        parse_ok(self, B.html_section(data))


class Html(unittest.TestCase):
    def test_section(self):
        self.assertEqual(B.html_section({}), "")
        self.assertEqual(B.html_section({"board": None, "timing": {"ok": True}}), "")
        a = B.html_section(B.demo_results())
        b = B.html_section(B.demo_results())
        svg = parse_ok(self, a)
        self.assertEqual(svg.tag, "{http://www.w3.org/2000/svg}svg")
        pads = [e for e in svg.iter() if e.get("data-p")]
        self.assertEqual(len(pads), sum(len(p["pads"]) for p in B.load_board()["parts"].values()))
        uid = a[a.index("id='tfbv-") + 4:].split("'")[0]
        self.assertNotIn(uid, b)                                   # two blocks on one page do not clash
        data = a[a.index("class='tfbv-data'>") + 18:]
        fs = json.loads(data[:data.index("</script>")].replace("<\\/", "</"))
        self.assertEqual(len(fs), 6)
        self.assertNotIn("http", a.replace("http://www.w3.org/2000/svg", ""))   # offline: nothing fetched

    def test_page_and_report_hook(self):
        parse_ok(self, B.page(B.demo_results()))
        with tempfile.TemporaryDirectory() as t:
            d = Path(t)
            (d / "board").mkdir()
            (d / "board" / "board.json").write_text(json.dumps(B.demo_results()["board"]))
            report.write(d)
            h = (d / "report.html").read_text()
            self.assertIn("class='tfbv'", h)
            self.assertIn("DD&lt;4&gt; shorted to DD&lt;5&gt;", h)
            parse_ok(self, h)
            (d / "board" / "board.json").unlink()
            report.write(d)
            self.assertNotIn("class='tfbv'", (d / "report.html").read_text())


if __name__ == "__main__":
    unittest.main()
