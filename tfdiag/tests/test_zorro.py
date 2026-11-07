"""Expansion board list (exerciser tests 0x04-0x06)."""

import json
import unittest

from tfdiag.tests import helpers
from tfdiag import report, zorro


def res(test, size, addr, exp, got):
    return {"test": test, "size": size, "flags": ["info"], "failure": False,
            "addr": f"0x{addr:08x}", "expected": f"0x{exp:08x}", "got": f"0x{got:08x}"}


ZZ = res(0x04, 0x80, 0x1234, 0x82046000, 0x6D6E0000)          # MNT Z3, extended, 64 MB, no shut up
CFG = res(0x05, 0, 0x40000000, 0x04000000, 0xFF000000)
A2091 = res(0x04, 1, 0x1, 0xD1030000, 0x02020010)             # Commodore Z2 I/O, boot ROM, 64K
END = res(0x06, 2, 0, 0, 0)


class Decode(unittest.TestCase):
    def test_boards(self):
        zb = zorro.decode([ZZ, CFG, A2091, END])
        self.assertEqual(len(zb["boards"]), 2)
        z, a = zb["boards"]
        self.assertEqual((z["zorro"], z["manufacturer"], z["product"], z["size"]), ("III", 28014, 4, 64 << 20))
        self.assertTrue(z["no_shutup"])
        self.assertEqual(z["configured_at"], "0x40000000")
        self.assertEqual((a["zorro"], a["manufacturer_name"], a["size"], a["boot_rom"]),
                         ("II", "Commodore (West Chester)", 64 << 10, True))
        self.assertEqual(a["diag_vec"], "0x0010")
        self.assertEqual(zb["end"]["reason"], 0)
        self.assertIn("MNT 28014", zorro.describe(z))
        self.assertIn("configured at 0x40000000", zorro.describe(z))

    def test_rerun_after_a_hang_uses_the_first_complete_list(self):
        zb = zorro.decode([ZZ, CFG, A2091, END, ZZ, CFG])
        self.assertEqual(zb["passes"], 2)
        self.assertEqual(len(zb["boards"]), 2)
        self.assertIsNotNone(zb["end"])

    def test_partial(self):
        zb = zorro.decode([ZZ])
        self.assertIsNone(zb["end"])

    def test_none(self):
        self.assertIsNone(zorro.decode([res(0x02, 0, 0, 0, 0)]))
        zb = zorro.decode([END | {"size": 0}])
        self.assertEqual(zb["boards"], [])
        self.assertEqual(zb["end"]["count"], 0)

    def test_failures_not_taken(self):
        bad = dict(ZZ, flags=[], failure=True)
        self.assertIsNone(zorro.decode([bad]))

    def test_sizes(self):
        self.assertEqual(zorro.board_size(False, 0xC0, 0), 8 << 20)
        self.assertEqual(zorro.board_size(False, 0xE6, 0), 2 << 20)
        self.assertEqual(zorro.board_size(True, 0x80, 0), 16 << 20)
        self.assertEqual(zorro.board_size(True, 0x87, 0x20), 16 << 20)    # code 7 reserved
        self.assertEqual(zorro.board_size(True, 0x83, 0x20), 128 << 20)


class EndToEnd(helpers.Base):
    state = {"ex": {"hang_preset": None, "errors": [], "phases_per_preset": 3, "no_body_once": False,
                    "stale_dsack_preset": None, "version": 3,
                    "zorro": [[True, 28014, 4, 0x82, 0x60, 0x40000000], [False, 514, 3, 0xD1, 0x00, None]]}}

    def test_run_lists_boards(self):
        out = self.env.tmp / "job"
        rc, evs = helpers.run_cli(self.env, ["run", "--images", str(self.env.images), "--out", str(out),
                                             "--presets", "0,2", "--secs", "0.2", "--no-board"])
        self.assertIn(rc, (0, 1), evs[-1])
        tm = json.loads((out / "timing" / "timing.json").read_text())
        zb = tm["expansion_boards"]
        self.assertEqual([b["manufacturer"] for b in zb["boards"]], [28014, 514])
        rows = report.rows_for(report.collect(out))
        ebs = [r for r in rows if r[0] == "Expansion boards"]
        self.assertEqual(len(ebs), 3, ebs)
        self.assertIn("MNT", ebs[0][2])
        notes = [r for r in rows if r[0] == "Exerciser" and "expansion" in str(r[1])]
        self.assertEqual(notes, [])
        self.assertEqual(self.board()["forbidden"], [])


if __name__ == "__main__":
    unittest.main()
