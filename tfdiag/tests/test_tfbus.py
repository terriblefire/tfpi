"""The GPIO7 / CE0 SPI bus handshake with the TF4060 (tf4060v2 #123)."""
import os
import struct
import tempfile
import unittest

from tfdiag import spi as S


class Mem:
    """struct.pack_into / unpack_from target with side effects on the set/clear registers"""
    def __init__(self, ce0_seq):
        self.b = bytearray(4096)
        self.ce0_seq = list(ce0_seq)     # CE0 levels for successive GPLEV0 reads (last repeats)
        self.sel = 1
        self.events = []

    def lev(self):
        c = self.ce0_seq.pop(0) if len(self.ce0_seq) > 1 else self.ce0_seq[0]
        return (c << 8) | (self.sel << 7)


class Bus(S.TfBus):
    def __init__(self, ce0_seq):
        self.mem = Mem(ce0_seq)
        S.TfBus.__init__(self, self.mem.b)

    def _rd(self, off):
        if off == self.GPLEV0:
            return self.mem.lev()
        return struct.unpack_from("<I", self.mem.b, off)[0]

    def _wr(self, off, v):
        if off == self.GPSET0 and v & (1 << self.SEL):
            self.mem.sel = 1
            self.mem.events.append("sel-")
        elif off == self.GPCLR0 and v & (1 << self.SEL):
            self.mem.sel = 0
            self.mem.events.append("sel+")
        else:
            struct.pack_into("<I", self.mem.b, off, v & 0xFFFFFFFF)


def fsel(bus, gpio):
    reg = struct.unpack_from("<I", bus.mem.b, 4 * (gpio // 10))[0]
    return (reg >> (3 * (gpio % 10))) & 7


class TfBusTest(unittest.TestCase):
    def test_init_parks_pins_and_sel_idle(self):
        b = Bus([1])
        self.assertEqual(b.mem.sel, 1)
        self.assertEqual(fsel(b, 7), 1)                  # GPIO7 output
        self.assertEqual((fsel(b, 10), fsel(b, 11)), (0, 0))

    def test_acquire_release(self):
        b = Bus([1])
        b.mem.events.clear()
        b.acquire()
        self.assertEqual(b.mem.sel, 0)
        self.assertEqual((fsel(b, 10), fsel(b, 11)), (4, 4))   # ALT0 for the transfer
        b.release()
        self.assertEqual(b.mem.sel, 1)
        self.assertEqual((fsel(b, 10), fsel(b, 11)), (0, 0))
        self.assertEqual(b.mem.events, ["sel+", "sel-"])

    def test_waits_while_ce0_low(self):
        b = Bus([0, 0, 0, 1])                            # the Amiga has the bus for 3 polls
        b.mem.events.clear()
        b.acquire()
        self.assertEqual(b.mem.events, ["sel+"])         # GPIO7 only asserted once CE0 is high
        b.release()

    def test_backs_off_when_the_amiga_wins_the_race(self):
        b = Bus([1, 0, 1, 1])                            # high, then low right after GPIO7 went low
        b.mem.events.clear()
        b.acquire()
        self.assertEqual(b.mem.events, ["sel+", "sel-", "sel+"])
        self.assertEqual((fsel(b, 10), fsel(b, 11)), (4, 4))

    def test_gives_up_when_the_amiga_never_releases(self):
        b = Bus([0])
        b.WAIT_S = 0.05
        with self.assertRaises(S.SpiError):
            b.acquire()
        self.assertEqual(b.mem.sel, 1)
        self.assertEqual((fsel(b, 10), fsel(b, 11)), (0, 0))


class CsPinTest(unittest.TestCase):
    def dt(self, cs_pin, compat=b"raspberrypi,model-zero-2-w\0brcm,bcm2837\0"):
        d = tempfile.mkdtemp()
        os.makedirs(f"{d}/aliases")
        os.makedirs(f"{d}/soc/spi@7e204000")
        open(f"{d}/aliases/spi0", "wb").write(b"/soc/spi@7e204000\0")
        open(f"{d}/soc/spi@7e204000/cs-gpios", "wb").write(struct.pack(">6I", 0x10, cs_pin, 1, 0x10, 25, 1))
        open(f"{d}/compatible", "wb").write(compat)
        return d

    def test_cs_pin(self):
        self.assertEqual(S.spidev_cs_pin(self.dt(7)), 7)
        self.assertEqual(S.spidev_cs_pin(self.dt(26)), 26)
        self.assertIsNone(S.spidev_cs_pin(tempfile.mkdtemp()))

    def test_old_overlay_no_bus(self):
        self.assertIsNone(S.open_bus(self.dt(7)))

    def test_new_overlay_maps_gpiomem(self):
        f = tempfile.NamedTemporaryFile(delete=False)
        f.write(b"\0" * 4096)
        f.close()
        bus = S.open_bus(self.dt(26), gpiomem=f.name)
        self.assertIsInstance(bus, S.TfBus)

    def test_pi5_refused(self):
        with self.assertRaises(S.SpiError):
            S.open_bus(self.dt(26, compat=b"raspberrypi,5-model-b\0brcm,bcm2712\0"))


if __name__ == "__main__":
    unittest.main()
