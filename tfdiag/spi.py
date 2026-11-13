"""SPI to the RAM FPGA (spidev0.0): shared SRAM, diag commands, tprobe.

a314d must be stopped: it owns spidev0.0 when it runs.
Framing details live in protocol.py.
"""

import mmap
import os
import struct
import time

from . import protocol as P


class SpiError(RuntimeError):
    pass


class TfBus:
    """The SPI bus handshake with the TF4060's flash master (tf4060v2 #123).

    SCK/MOSI/MISO are shared with the FPGA, which reads the SPI ROM for the
    Amiga. The FPGA takes the bus only while our select (GPIO7) is idle and
    holds CE0 (GPIO8) low while it has it. With the a314 spi-a314 overlay that
    moves the kernel's chip select off GPIO7 (a314d drives GPIO7 itself), each
    transfer here does what a314d does: wait for CE0 high, assert GPIO7, check
    CE0 again ~2 us later (the Amiga may have got the bus in that instant) and
    back off if it is low. SCLK/MOSI are in ALT0 only for the transfer: the Pi's
    SPI block drives them whenever they are.
    """
    GPFSEL0, GPFSEL1, GPSET0, GPCLR0, GPLEV0 = 0x00, 0x04, 0x1C, 0x28, 0x34
    SEL, CE0 = 7, 8
    WAIT_S = 5.0                 # the Amiga holds CE0 longer only if it hung

    def __init__(self, mem):
        self.m = mem
        self._wr(self.GPSET0, 1 << self.SEL)                     # idle, then output
        self._wr(self.GPFSEL0, (self._rd(self.GPFSEL0) & ~(7 << 21)) | (1 << 21))
        self.pins(False)

    def _rd(self, off):
        return struct.unpack_from("<I", self.m, off)[0]

    def _wr(self, off, v):
        struct.pack_into("<I", self.m, off, v & 0xFFFFFFFF)

    def pins(self, spi):
        """GPIO10 (MOSI) and GPIO11 (SCLK): ALT0 (4) for a transfer, else inputs"""
        v = self._rd(self.GPFSEL1) & ~0x3F
        if spi:
            v |= (4 << 0) | (4 << 3)
        self._wr(self.GPFSEL1, v)

    def ce0_low(self):
        return not (self._rd(self.GPLEV0) >> self.CE0) & 1

    def acquire(self):
        t0 = time.monotonic()
        back = 1e-5
        while True:
            if not self.ce0_low():
                self._wr(self.GPCLR0, 1 << self.SEL)
                t = time.perf_counter()
                while time.perf_counter() - t < 2e-6:
                    pass
                if not self.ce0_low():
                    self.pins(True)
                    return
                self._wr(self.GPSET0, 1 << self.SEL)             # the Amiga got it first
            if time.monotonic() - t0 > self.WAIT_S:
                raise SpiError(f"the TF4060 has held the SPI bus (CE0) for {self.WAIT_S:.0f} s")
            time.sleep(back)
            back = min(back * 2, 1e-3)

    def release(self):
        self.pins(False)
        self._wr(self.GPSET0, 1 << self.SEL)


def spidev_cs_pin(dt="/proc/device-tree"):
    """The GPIO of spidev0.0's kernel chip select, from the device tree, or None"""
    try:
        node = open(f"{dt}/aliases/spi0", "rb").read().rstrip(b"\0").decode()
        cells = open(f"{dt}{node}/cs-gpios", "rb").read()
    except OSError:
        return None
    pins = struct.unpack(f">{len(cells) // 4}I", cells[:len(cells) // 4 * 4])
    return pins[1] if len(pins) >= 2 else None


def open_bus(dt="/proc/device-tree", gpiomem="/dev/gpiomem"):
    """TfBus when the kernel's chip select is not GPIO7 (the a314d SPI handshake
    overlay), None when the kernel still drives GPIO7 (older overlay)."""
    cs = spidev_cs_pin(dt)
    if cs is None or cs == TfBus.SEL:
        return None
    try:
        if b"bcm2712" in open(f"{dt}/compatible", "rb").read():
            raise SpiError("Pi 5: no /dev/gpiomem GPIO7 handshake")
        fd = os.open(gpiomem, os.O_RDWR | os.O_SYNC)
        try:
            mem = mmap.mmap(fd, 4096, mmap.MAP_SHARED, mmap.PROT_READ | mmap.PROT_WRITE)
        finally:
            os.close(fd)
    except OSError as e:
        raise SpiError(f"GPIO7 handshake: cannot map {gpiomem}: {e}") from e
    return TfBus(mem)


def open_device(cfg=None):
    """A spidev.SpiDev (or the test fake named by cfg.fake_spi)."""
    hz = min(int(getattr(cfg, "spi_hz", P.SPI_HZ) or P.SPI_HZ), P.SPI_MAX_HZ)
    if cfg is not None and getattr(cfg, "fake_spi", ""):
        from .tests import fakes  # noqa: only reached in tests
        return fakes.FakeSpiDev(cfg.fake_spi)
    try:
        import spidev
    except ImportError as e:
        raise SpiError("python3-spidev is not installed") from e
    dev = spidev.SpiDev()
    try:
        dev.open(P.SPI_BUS, P.SPI_DEV)
    except OSError as e:
        raise SpiError(f"cannot open spidev{P.SPI_BUS}.{P.SPI_DEV}: {e}") from e
    dev.mode = P.SPI_MODE
    dev.max_speed_hz = hz
    return dev


def find_magic(resp, magic, search=P.MAGIC_SEARCH):
    i = resp.find(bytes([magic]), 1)
    if i < 0 or i > search:
        return None
    return i


class Spi:
    def __init__(self, dev, bus=None):
        self.dev = dev
        self.bus = bus           # TfBus: we drive GPIO7 (see open_bus)

    def close(self):
        try:
            self.dev.close()
        except Exception:
            pass
        if self.bus:
            self.bus.release()

    slow = False                 # set by the timing run around clock switches: SPI at SPI_SLOW_HZ

    def xfer(self, data, speed_hz=None):
        if self.slow:
            speed_hz = min(speed_hz or P.SPI_SLOW_HZ, P.SPI_SLOW_HZ)
        if self.bus:
            self.bus.acquire()
        try:
            if speed_hz:
                return bytes(self.dev.xfer2(list(data), int(speed_hz)))
            return bytes(self.dev.xfer2(list(data)))
        finally:
            if self.bus:
                self.bus.release()

    # ---- shared SRAM ---------------------------------------------------------
    def sram_read(self, addr, n):
        out = bytearray()
        while n > 0:
            k = min(n, P.SPI_CHUNK)
            a = addr & 0x3FFF
            hdr = [P.SRAM_READ | ((a >> 16) & 0x1F), (a >> 8) & 0xFF, a & 0xFF]
            r = self.xfer(hdr + [0] * (P.SRAM_READ_DATA_AT - 3 + k))
            out += r[P.SRAM_READ_DATA_AT:P.SRAM_READ_DATA_AT + k]
            addr += k
            n -= k
        return bytes(out)

    def sram_write(self, addr, data):
        data = bytes(data)
        off = 0
        while off < len(data):
            k = min(len(data) - off, P.SPI_CHUNK)
            a = (addr + off) & 0x3FFF
            self.xfer([P.SRAM_WRITE | ((a >> 16) & 0x1F), (a >> 8) & 0xFF, a & 0xFF] + list(data[off:off + k]))
            off += k

    def u32(self, addr):
        return struct.unpack(">I", self.sram_read(addr, 4))[0]

    def u16(self, addr):
        return struct.unpack(">H", self.sram_read(addr, 2))[0]

    def proto_version(self):
        r = self.xfer([P.PROTO_VER, 0, 0, 0])
        return r[3]

    # ---- diag commands (DIAG builds only) ---------------------------------------
    def reset_assert(self):
        self.xfer([P.CMD_RESET_ASSERT])

    def reset_release(self):
        self.xfer([P.CMD_RESET_RELEASE])

    def diag_id(self):
        """{'version','features':[...],'feature_bits','commit'} or None (not a DIAG build)."""
        r = self.xfer([P.CMD_DIAG_ID] + [0] * (P.MAGIC_SEARCH + P.DIAG_ID_LEN))
        i = find_magic(r, P.MAGIC_DIAG_ID)
        if i is None:
            return None
        d = r[i:]
        bits = d[2]
        return {"version": d[1], "feature_bits": bits,
                "features": [n for b, n in P.FEATURES.items() if bits >> b & 1],
                "commit": d[3:7].hex()}

    def clocks_latch(self):
        """Latch and read the clock counters: {field: value} per protocol.CLOCK_FRAME."""
        n = sum(w for _, w in P.CLOCK_FRAME)
        r = self.xfer([P.CMD_CLOCKS] + [0] * (P.MAGIC_SEARCH + n))
        i = find_magic(r, P.MAGIC_CLOCKS)
        if i is None:
            raise SpiError("no clock counter magic (0xA4): not a DIAG build?")
        out, o = {}, i + 1
        for name, w in P.CLOCK_FRAME:
            out[name] = int.from_bytes(r[o:o + w], "big")
            o += w
        return out

    def cycle_counters(self):
        """0xA6: {field: value} per protocol.CYCLE_FRAME."""
        n = sum(w for _, w in P.CYCLE_FRAME)
        r = self.xfer([P.CMD_CYCLES] + [0] * (P.MAGIC_SEARCH + n))
        i = find_magic(r, P.MAGIC_CYCLES)
        if i is None:
            raise SpiError("no cycle counter magic (0xA6)")
        out, o = {}, i + 1
        for name, w in P.CYCLE_FRAME:
            out[name] = int.from_bytes(r[o:o + w], "big")
            o += w
        return out

    def cycle_clear(self, threshold=None):
        self.xfer([P.CMD_CYCLES_CLEAR] + ([threshold & 0xFF] if threshold is not None else []))

    # ---- timing histograms (0xA8 / 0xA9, bins in the SRAM) -------------------------
    def hist_start(self, shift, region):
        """Clear all 256 counters and bin region `region` with bin shift `shift`."""
        self.xfer([P.CMD_HIST_START, shift & 3, region & 7])

    def hist_status(self):
        n = sum(w for _, w in P.HIST_FRAME)
        r = self.xfer([P.CMD_HIST_STATUS] + [0] * (P.MAGIC_SEARCH + n))
        i = find_magic(r, P.MAGIC_HIST)
        if i is None:
            raise SpiError("no histogram magic (0xA9)")
        out, o = {}, i + 1
        for name, w in P.HIST_FRAME:
            out[name] = int.from_bytes(r[o:o + w], "big")
            o += w
        out["clearing"] = bool(out["status"] & P.HIST_STATUS_CLEARING)
        return out

    def hist_bins(self, reads=3):
        """[[32 counts] x 8]. The FPGA updates the counters by read-modify-write while
        the Pi reads, so a read can catch a half-updated counter: read `reads` times and
        take the median of each counter."""
        n = P.HIST_MEASUREMENTS * P.HIST_BINS
        snaps = []
        for _ in range(max(1, reads)):
            lo = self.sram_read(P.HIST_LO, n)
            hi = self.sram_read(P.HIST_HI, n)
            snaps.append([lo[i] | (hi[i] << 8) for i in range(n)])
        vals = [sorted(s[i] for s in snaps)[len(snaps) // 2] for i in range(n)]
        return [vals[m * P.HIST_BINS:(m + 1) * P.HIST_BINS] for m in range(P.HIST_MEASUREMENTS)]

    def wave_arm(self, kind, region=0, log=False):
        """Arm the waveform; with log=True (0xB4) the address log too (body running only)."""
        self.xfer([P.CMD_WAVE_LOG_ARM if log else P.CMD_WAVE_ARM, kind & 0xFF, region & 0xFF])

    def log_status(self):
        n = sum(w for _, w in P.LOG_FRAME)
        r = self.xfer([P.CMD_LOG_STATUS] + [0] * (P.MAGIC_SEARCH + n))
        i = find_magic(r, P.MAGIC_LOG)
        if i is None:
            raise SpiError("no address log magic (0xB5)")
        out, o = {}, i + 1
        for name, w in P.LOG_FRAME:
            out[name] = int.from_bytes(r[o:o + w], "big")
            o += w
        st = out["status"]
        out.update(armed=bool(st & P.LOG_STATUS_ARMED), frozen=bool(st & P.LOG_STATUS_FROZEN),
                   wrapped=bool(st & P.LOG_STATUS_WRAPPED), trigger_seen=bool(out["trigger"] & 0x400),
                   trigger_entry=out["trigger"] & 0x3FF)
        return out

    def log_entries(self, status):
        """Address log entries in time order (read only once the log is frozen)."""
        raw = self.sram_read(P.LOG_BASE, P.LOG_ENTRIES * P.LOG_ENTRY_BYTES)
        out = []
        for j in range(min(status["count"], P.LOG_ENTRIES)):
            e = (status["first"] + j) % P.LOG_ENTRIES
            ent = P.log_entry(raw[e * P.LOG_ENTRY_BYTES:(e + 1) * P.LOG_ENTRY_BYTES])
            ent["index"] = e
            out.append(ent)
        return out

    def wave_stop(self):
        self.xfer([P.CMD_WAVE_STOP])

    def wave_status(self):
        r = self.xfer([P.CMD_WAVE_READ] + [0] * (P.MAGIC_SEARCH + 12))
        i = find_magic(r, P.MAGIC_WAVE)
        if i is None:
            raise SpiError("no waveform magic (0xB1)")
        st = r[i + 1]
        count, first, trig, wp = struct.unpack(">HHHH", r[i + 2:i + 10])
        return {"armed": bool(st & P.WAVE_STATUS_ARMED), "triggered": bool(st & P.WAVE_STATUS_TRIGGERED),
                "full": bool(st & P.WAVE_STATUS_FULL), "status": st, "count": count,
                "first": first, "trigger": trig, "wp": wp, "kind": r[i + 10], "region": r[i + 11]}

    def wave_entries(self, status):
        """Entries in time order: list of (signals, duration_clks)."""
        raw = self.sram_read(P.WAVE_BASE, P.WAVE_SIZE)
        n = min(status["count"], P.WAVE_ENTRIES)
        out = []
        for j in range(n):
            e = (status["first"] + j) % P.WAVE_ENTRIES
            v = 0
            for k in range(P.WAVE_ENTRY_BYTES):
                v = (v << 8) | raw[P.wave_addr(e, k) - P.WAVE_BASE]
            out.append((v >> P.WAVE_DUR_BITS, P.wave_duration(v & ((1 << P.WAVE_DUR_BITS) - 1))))
        return out

    # ---- tprobe -------------------------------------------------------------
    def tprobe_select(self, meas, region=0):
        self.xfer([P.TPROBE_REGION | (region & 7), 0, 0])
        self.xfer([P.TPROBE_SEL | (meas & 0x1F), 0, 0])

    def tprobe_clear(self):
        self.xfer([P.TPROBE_CLEAR, 0, 0])

    def tprobe_read(self):
        r = self.xfer([P.TPROBE_READ] + [0] * 80)
        i = find_magic(r, P.TPROBE_MAGIC)
        if i is None:
            raise SpiError("no tprobe magic (0x80)")
        d = r[i:]
        w = [int.from_bytes(d[3 + 2 * k:5 + 2 * k], "big") for k in range(2 + P.TPROBE_BINS)]
        return {"sel": d[1], "region": d[2], "starts": w[0], "samples": w[1], "bins": w[2:]}

    # ---- pin readback image -------------------------------------------------
    def pinread(self, order=None):
        """{fpga_pin: level} from the diag_pins image, or None if it isn't loaded."""
        order = order or P.PINREAD_ORDER
        r = self.xfer([0] * P.PINREAD_LEN, P.PINREAD_HZ)
        if r[0] != P.PINREAD_MAGIC:
            return None
        bits = int.from_bytes(r[1:], "big")
        nbits = 8 * (len(r) - 1)
        return {pin: (bits >> (nbits - 1 - k)) & 1 for k, pin in enumerate(order)}


def sleep(s):
    time.sleep(s)
