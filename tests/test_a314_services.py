"""install.sh appends the services a314 has added since the Pi's a314d.conf was
written (install-pi.sh writes that file only when it is missing). The section
runs here against a scratch root, with fetch() as a stand-in."""
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
START = "# --- a314 services added"
END = "# --- tfpi web page"

def line(name, script=None):
    return "%s\t\t/opt/a314/venv/bin/python3\t/opt/a314/%s.py\n" % (name, script or name)


# a314d.conf as install-pi.sh wrote it on 8 Nov 2026 (the T's Pi), and as a314 has it now
OLD = "".join(line(n) for n in ("a314fs", "picmd", "piaudio", "remotewb", "remote-mouse",
                                "videoplayer", "ethernet", "disk", "hid"))
NEW = OLD.replace(line("hid"), line("scsi") + line("hid")) \
    + line("pistats") + line("a314sh", "a314shd") + line("usbhw", "usbbridge")
NEW_ONLY = line("scsi") + line("pistats") + line("a314sh", "a314shd") + line("usbhw", "usbbridge")

# fetch NAME REPO REF OWNER: "clones" the checkout from $FIXTURE, or fails
FETCH = """
fetch() {
	echo "fetch $*" >> "$CALLS"
	[ -n "${FETCH_FAILS:-}" ] && { echo "fatal: no network" >&2; return 1; }
	mkdir -p "$SRC/$1/Software/a314d" && cp "$FIXTURE" "$SRC/$1/Software/a314d/a314d.conf"
}
"""


def section():
    text = (ROOT / "install.sh").read_text()
    return text[text.index(START):text.index(END)]


class A314Services(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(subprocess.run, ["rm", "-rf", str(self.tmp)])
        self.etc = self.tmp / "etc/opt/a314"
        self.etc.mkdir(parents=True)
        self.src = self.tmp / "src"
        self.fixture = self.tmp / "a314d.conf.new"
        self.fixture.write_text(NEW)
        self.calls = self.tmp / "calls"

    def checkout(self):
        d = self.src / "a314/Software/a314d"
        d.mkdir(parents=True)
        (d / "a314d.conf").write_text(NEW)

    def run_section(self, **env):
        script = section().replace("/etc/opt/a314", str(self.etc))
        e = dict(os.environ, SRC=str(self.src), FIXTURE=str(self.fixture), CALLS=str(self.calls),
                 A314_REPO="https://github.com/erique/a314", A314_COMMIT="758dabc", A314_USER="pi", **env)
        r = subprocess.run(["bash", "-c", "set -euo pipefail\nSRC=$SRC\n" + FETCH + script + "\necho done"],
                           env=e, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(r.stdout.endswith("done\n"), r.stdout)
        return r.stdout

    def conf(self):
        return (self.etc / "a314d.conf").read_text()

    def fetched(self):
        return self.calls.read_text().splitlines() if self.calls.exists() else []

    def test_new_services_appended_once(self):
        (self.etc / "a314d.conf").write_text(OLD)
        self.checkout()
        out = self.run_section()
        self.assertIn("services added to %s/a314d.conf: scsi pistats a314sh usbhw" % self.etc, out)
        self.assertEqual(self.conf(), OLD + NEW_ONLY)
        self.assertEqual(self.fetched(), [])            # the checkout was there
        out = self.run_section()                       # a second run adds nothing
        self.assertIn("has every service", out)
        self.assertEqual(self.conf(), OLD + NEW_ONLY)

    def test_user_lines_kept(self):
        mine = OLD.replace("/opt/a314/a314fs.py", "/opt/a314/a314fs.py --debug") \
            + "#" + line("usbhw", "usbbridge") + "mine\t/usr/bin/python3\t/home/pi/mine.py"
        (self.etc / "a314d.conf").write_text(mine)          # no newline at the end
        self.checkout()
        self.run_section()
        added = "".join(l for l in NEW_ONLY.splitlines(True) if not l.startswith("usbhw"))
        self.assertEqual(self.conf(), mine + "\n" + added)

    def test_missing_checkout_fetched_again(self):
        (self.etc / "a314d.conf").write_text(OLD)
        out = self.run_section()
        self.assertEqual(self.fetched(), ["fetch a314 https://github.com/erique/a314 758dabc pi"])
        self.assertIn("scsi pistats a314sh usbhw", out)
        self.assertEqual(self.conf(), OLD + NEW_ONLY)

    def test_fetch_fails(self):
        (self.etc / "a314d.conf").write_text(OLD)
        out = self.run_section(FETCH_FAILS="1")
        self.assertIn("services not checked", out)
        self.assertEqual(self.conf(), OLD)

    def test_no_pi_conf(self):
        self.checkout()                                # install-pi.sh writes it on a fresh install
        out = self.run_section()
        self.assertIn("services not checked", out)
        self.assertFalse((self.etc / "a314d.conf").exists())
        self.assertEqual(self.fetched(), [])


if __name__ == "__main__":
    unittest.main()
