"""Every FPGA/exerciser wire detail tfdiag depends on, in one place.

The RTL (rtl/diag/, rtl/a314.v) and the exerciser (rom/diagex*.asm) own these
formats; docs/DIAGNOSTICS.md is the shared spec. When the RTL changes a frame,
change it here and nowhere else. Items marked PROVISIONAL were not yet fixed in
DIAGNOSTICS.md when this was written.
"""

# ---- SPI link ---------------------------------------------------------------
SPI_BUS, SPI_DEV = 0, 0
SPI_MODE = 0
SPI_HZ = 4_000_000          # a314.v takes up to 25 MHz; the spec says <= 12 MHz
SPI_MAX_HZ = 12_000_000
SPI_CHUNK = 2048            # spidev bufsiz is 4096 by default

# ---- A314 shared SRAM (SPI addresses; SRAM offset = address + 0x400) ----------
SRAM_READ = 0x00            # 000aaaaa aaaaaaaa aaaaaaaa -> data from byte 5
SRAM_READ_DATA_AT = 5
SRAM_WRITE = 0x20           # 001aaaaa aaaaaaaa aaaaaaaa <- data from byte 3
SRAM_SIZE = 0x4000
PROTO_VER = 0xFF            # -> 0x02 at byte 3

BODY_BASE = 0x0000          # exerciser body (uploaded by the Pi, Amiga held in reset)
BODY_MAX = 0x2000
WAVE_BASE = 0x2000          # 0x2000-0x25FF waveform entries (written by the FPGA)
WAVE_SIZE = 0x0600
# 0x2600-0x26FF / 0x2800-0x28FF histogram counters (low / high bytes); 0x2A00-0x2BFF free
MAILBOX = 0x2C00            # Amiga 0x00E93000
MAILBOX_SIZE = 0x1000

# Body image at BODY_BASE, written unchanged from diagex_body.bin (rom/diagbody.py):
# 16-byte header {'TFDB', u32 code length (multiple of 4), u32 checksum, u32 load
# address}, then the code. Checksum over the code as big-endian longs:
# sum = rol32(sum, 1) + long.
BODY_MAGIC = b"TFDB"
BODY_HEADER = 16


def body_sum(code):
    s = 0
    for k in range(0, len(code) - len(code) % 4, 4):
        s = ((s << 1) | (s >> 31)) & 0xFFFFFFFF
        s = (s + int.from_bytes(code[k:k + 4], "big")) & 0xFFFFFFFF
    return s


# ---- Mailbox (offsets from MAILBOX; big-endian; rom/diagex.i) ---------------------
MB_MAGIC = 0x000            # 'TFDX': written by the boot ROM stub as soon as it runs
MB_HEARTBEAT = 0x004
MB_PHASE = 0x008            # u16 group id running; 0xFxxx stub stages; 0xFFFF done
MB_PRESET = 0x00A           # u16 CLKSEL preset in force
MB_STATUS = 0x00C           # u32 flags (STB_*)
MB_VERSION = 0x010          # u16 exerciser version
MB_NRES = 0x012             # u16 result entries written
MB_NFAIL = 0x014            # u32 failures seen
MB_PDONE = 0x018            # u32 bit n: preset list index n completed
MB_PFAIL = 0x01C            # u32 bit n: preset list index n saw a failure
MB_PIDX = 0x020             # u16 preset list index running
MB_ITER = 0x022
MB_XPC = 0x024              # last exception: PC, fault address, vector offset, count
MB_XADDR = 0x028
MB_XVEC = 0x02C
MB_XCOUNT = 0x02E
MB_INTS = 0x030
MB_BOOTS = 0x038            # u32 stub boot count (SRAM survives resets)
MB_STUBVER = 0x03C
MB_PLANSRC = 0x03E          # 1 Pi plan, 2 default plan
MB_HEADER_LEN = 0x40
MB_MACHINE = 0x040          # 64 bytes machine ID block
MB_MACHINE_LEN = 0x40
MB_RESULTS = 0x100
MB_RESULTS_END = 0x800
MB_RESULT_LEN = 16          # {u16 preset<<8|test, u8 size, u8 flags, u32 addr, u32 expected, u32 got}
MB_GO = 0x800               # 'TFGO'
MB_MASK = 0x804             # u32 test mask (0 = all)
MB_NPRE = 0x808             # u16 preset count
MB_PRE = 0x80A              # u8[32] presets
MB_MAX_PRESETS = 32
MB_START = 0x82A            # u16 start at preset list index N
MB_ITERS = 0x82C            # u16 iterations per test (0 = 8)
MB_FLAGS = 0x82E            # u16 bit0 append: keep earlier results
MB_PLAN_END = 0x830
# Hold mode (Pi -> Amiga): the exerciser loops one test group so the Pi can measure it.
#   0x830 u16 hold group = test-mask bit number + 1 (0 = no hold), 0x832 u16 hold sequence
#   (the Pi bumps it with every new request). The exerciser echoes the group and sequence it
#   is looping into MB_HOLD_ECHO / MB_HOLD_ECHO_SEQ.
# Write 0x832 then 0x830; the group is running once 0x080/0x082 echo both. 0x084 u32 counts
# the loops of the held group. Group numbers: 1 chip, 2 custom, 3 CIA, 4 ROM, 5 mb fast RAM,
# 6 TAS/CAS, 7 interrupts, 9 clock measurement, 10 LED, 11 mb registers.
MB_HOLD = 0x830
MB_HOLD_SEQ = 0x832
MB_HOLD_ECHO = 0x080
MB_HOLD_ECHO_SEQ = 0x082
MB_HOLD_LOOPS = 0x084
# Clock switch handshake: before a CLKSEL switch the body writes 0x8000 | count << 8 | preset
# to 0x088 and waits (~1 s) for the Pi to copy it to 0x834; the Pi then makes no SPI transfer
# for SWITCH_QUIET_S (the FPGA runs on the 25 MHz PLL bypass clock meanwhile and garbles fast
# SPI). Bit 15 of 0x088 clears when that switch is done. The 0x01 info entry's addr has
# bit 8 set when the Pi acked.
MB_SWITCH = 0x088
MB_SWITCH_ACK = 0x834
SWITCH_PENDING = 0x8000
SWITCH_QUIET_S = 0.05
SWITCH_POLL_S = 0.2          # poll 0x088 at least this often during phase 0x0001
SPI_SLOW_HZ = 2_000_000      # every transfer made while phase is 0x0001 or a switch is pending
# Exerciser (body) version 2 = hold mode + the clock switch handshake (0x088 / 0x834).
# Older bodies: no hold requests and no handshake, only slow SPI during phase 0x0001.
# The manifest can still say "exerciser_hold": false.
HOLD_MIN_VERSION = 2
# phases during which a hold request is not acted on (cannot be interrupted)
NO_HOLD_PHASES = {0x0001, 0x0003, 0x0004}
FLAG_APPEND = 1
MAGIC_RUNNING = b"TFDX"
MAGIC_GO = b"TFGO"
PHASE_DONE = 0xFFFF
STB = {"done": 0, "error": 1, "resumed": 2, "overflow": 3, "defplan": 4, "fatal": 5, "nobody": 6}
STATUS_DONE = 1 << STB["done"]
STATUS_FATAL = 1 << STB["fatal"]
STATUS_NOBODY = 1 << STB["nobody"]
STATUS_BODY_RUNNING = 1 << 7    # set by the body at its first instruction: SPI 0x0000-0x1FFF is free
# stub stages (MB_PHASE)
PH_STUB, PH_WAIT, PH_BADBODY, PH_COPY, PH_BODY = 0xF000, 0xF001, 0xF002, 0xF003, 0xF004
PH_COPYBAD, PH_STUBEXC = 0xF005, 0xF0EE
STUB_STAGES = {PH_STUB: "stub entered", PH_WAIT: "waiting for TFGO and a valid body",
               PH_BADBODY: "body header or checksum bad", PH_COPY: "copying the body to chip RAM",
               PH_BODY: "body started", PH_COPYBAD: "chip RAM did not keep the body copy (halted)",
               PH_STUBEXC: "the stub took an exception (halted)"}
# test groups (MB_PHASE values and the low byte of a result's test field)
CLOCK_SWITCH_TEST = 0x01       # info entry per switch: addr = preset, expected = polls, got = bus errors
GROUPS = {0x01: "clock switch", 0x02: "CPU clock measurement", 0x03: "machine ID",
          0x04: "expansion boards", 0x05: "expansion board configured", 0x06: "expansion board chain end",
          0x10: "chip RAM",
          0x14: "TAS/CAS", 0x30: "motherboard fast RAM", 0x40: "custom registers", 0x50: "CIA registers",
          0x58: "CIA timer interrupts", 0x60: "ROM reads", 0x68: "motherboard chip registers"}
# (the SDMAC/SCSI DMA test, ids 0x70-0x73, was removed from the exerciser)
# test mask bits (MB_MASK)
TEST_BITS = {0: "chip", 1: "custom", 2: "cia", 3: "rom", 4: "mbram", 5: "locked", 6: "irq", 8: "clkmeas",
             9: "cia_led", 10: "mbregs"}
TEST_MASK_DEFAULT = 0x77F   # what mask 0 means to the exerciser
# result flags
RF = {"exception": 0, "timeout": 1, "summary": 2, "info": 3, "skip": 4}
RF_NOT_FAILURE = (1 << RF["info"]) | (1 << RF["skip"])

# machine ID block (offsets inside the 64-byte block at MB_MACHINE): name, offset, size
MACHINE_FIELDS = [
    ("ramsey_version", 0x00, 1), ("ramsey_ctl_reset", 0x01, 1), ("ramsey_ctl", 0x02, 1),
    ("gary_id", 0x03, 1), ("fat_gary_timeout", 0x04, 1), ("fat_gary_timeout_en", 0x05, 1),
    ("fat_gary_coldstart", 0x06, 1), ("probe_flags", 0x07, 1), ("denise_id1", 0x08, 2),
    ("denise_id2", 0x0A, 2), ("vposr", 0x0C, 2), ("buster", 0x0E, 2), ("chip_ram_bytes", 0x10, 4),
    ("mb_fast_base", 0x14, 4), ("mb_fast_bytes", 0x18, 4), ("kick_version", 0x1C, 2),
    ("kick_revision", 0x1E, 2), ("kick_checksum", 0x20, 4), ("pcr_060", 0x24, 4), ("tf_build", 0x28, 1),
    ("clksel_at_entry", 0x29, 1), ("rom_model", 0x2A, 1), ("mb_blocks", 0x38, 2),
]

# ---- Diag commands (0xA0-0xBF, DIAG builds; rtl/diag/diag.v) ----------------------
# Readouts: the magic is at byte 2 of the transfer, then the fields.
CMD_RESET_RELEASE = 0xA0
CMD_RESET_ASSERT = 0xA1
CMD_DIAG_ID = 0xA2
CMD_CLOCKS = 0xA4
CMD_CYCLES = 0xA6           # cycle counters readout
CMD_CYCLES_CLEAR = 0xA7     # clear; optional byte: fast-DSACK threshold (fabric clks)
CMD_HIST_START = 0xA8       # 0xA8 s r: bin shift 0-3, region 0-6; clears all 256 counters
CMD_HIST_STATUS = 0xA9
CMD_WAVE_ARM = 0xB0
CMD_WAVE_READ = 0xB1
CMD_WAVE_STOP = 0xB2         # also freezes the address log
CMD_WAVE_LOG_ARM = 0xB4      # 0xB4 k r: waveform arm (as 0xB0) plus the address log
CMD_LOG_STATUS = 0xB5
MAGIC_DIAG_ID = 0xD1
MAGIC_CLOCKS = 0xD4
MAGIC_CYCLES = 0xD6
MAGIC_HIST = 0xDA
MAGIC_WAVE = 0xCE
MAGIC_LOG = 0xCF
MAGIC_SEARCH = 8            # the magic is looked for in the first N response bytes
DIAG_ID_LEN = 1 + 1 + 1 + 4  # magic, version, features, commit
FEATURES = {0: "tprobe", 1: "waveform", 2: "clock_counters", 3: "reset", 4: "pinread", 5: "histograms",
            6: "cycle_counters", 7: "sdram_parked"}
# bit 7 (diag_pins.bin): the SDRAMs are parked (CS# high, CKE low, DQM high), so their DQ
# pins cannot drive: only then does the EXTEST drive nets shared with the SDRAMs
# default diag image 0x6E, DIAG_TPROBE=1 variant 0x6F, diag_pins.bin 0x10

# 0xA4: magic, then these big-endian fields in order. The gate is a fixed window
# of fabric clks (2^22); "free" is the free-running fabric clk count taken at the
# command, so two reads a Pi-timed interval apart give the fabric clock.
CLOCK_FRAME = [("CLKAUX", 4), ("gate", 4), ("free", 4), ("seq", 1)]
CLOCKS = [
    # counted clock, nominal MHz (None = no fixed expectation), what it is
    ("CLKAUX", 25.0, "25 MHz oscillator OSC2, second FPGA input (CLKXTAL feeds the PLL)"),
]
COUNTER_MODE = "window_free"

# 0xA6: magic, then these big-endian fields (rtl/diag/diag.v f_cc, diag_wave.v):
# counts saturate; min/max in fabric clks (min 0xFF / max 0 = none seen)
CYCLE_FRAME = [
    ("as", 3),            # AS20 falls
    ("sterm", 3),         # TA_BUS falls with no DSACK seen (STERM-terminated)
    ("dsack", 3),         # TA_BUS falls with a DSACK seen
    ("fast", 3),          # ... with DSACK fall -> TA_BUS fall <= threshold
    ("sterm_min", 1), ("sterm_max", 1),        # AS20 fall -> TA_BUS fall, STERM cycles
    ("dsack_ta_min", 1), ("dsack_ta_max", 1),  # DSACK fall -> TA_BUS fall
    ("threshold", 1),
]
CYCLE_THRESHOLD = 2

# 0xA8/0xA9 timing histograms: all 8 measurements binned at once for one region.
# Bin b of measurement m: low byte at SPI HIST_LO + 32*m + b, high byte at HIST_HI + 32*m + b.
# A sample goes into bin min(31, delta >> s); deltas saturate at 255 clks before the
# shift; counters are 16-bit, saturating. Times are fabric clks at the running preset.
HIST_LO = 0x2600
HIST_HI = 0x2800
HIST_MEASUREMENTS = 8
HIST_BINS = 32
HIST_SATURATE = 0xFFFF
HIST_FRAME = [("shift", 1), ("region", 1), ("status", 1), ("as", 3), ("lost", 2)]
HIST_STATUS_CLEARING = 1
HIST_BIN_TARGET = 50000     # keep the fullest bin of one 0xA8 window below this
# m: (name, bin-0 meaning or None when bin 0 is just the shortest delays)
HIST_MEAS = [
    ("DS20 -> DSACK, reads", None),
    ("DS20 -> DSACK, writes", None),
    ("AS20 -> STERM (TA_BUS, no DSACK)", None),
    ("AS20 -> TA_BUS, DSACK cycles", None),
    ("DS20 -> read data settled (last D[7:0] change)", "no D change after the DS20 fall"),
    ("DSACK -> TA_BUS", "same clk: DSACK with a fast TA_BUS (STERM as well?)"),
    ("AS20 rise -> DSACK release", "already released at the AS20 rise"),
    ("AS20 fall -> stale DSACK release", "not stale"),
]
# histogram / waveform region codes (the 060 address A31-A16 at the AS20 fall)
HIST_REGIONS = {"all": 0, "chip": 1, "cia": 2, "custom": 3, "rom": 4, "other": 5, "ramsey": 6}
FABRIC_MHZ = 100.0          # nominal PLL clock (25 MHz * 32 / 8)
FABRIC_TOLERANCE = 0.01

# 0xB0 trigger kinds
TRIGGERS = {
    0: "immediate",
    1: "AS20 fall",
    2: "TS fall",
    3: "AS20 fall in region",
    4: "TA_BUS end, no DSACK (STERM)",
    5: "RESET fall",
}
# 0xB1: magic, status, then u16 count, first, trigger index, write pointer, u8 kind, u8 region
WAVE_STATUS_ARMED, WAVE_STATUS_TRIGGERED, WAVE_STATUS_FULL = 1, 2, 4   # bit2 = done
WAVE_ENTRIES = 512
WAVE_PRETRIGGER = 128       # entries before the trigger entry that survive (384 from it on)
WAVE_ENTRY_BYTES = 3        # byte 0 = state[11:4], 1 = {state[3:0], dur[11:8]}, 2 = dur[7:0]
WAVE_SIG_BITS = 12
WAVE_DUR_BITS = 12
WAVE_SLICE = 0x200          # byte k of entry e at WAVE_BASE + k*0x200 + e
# signal bit n of the 12 state bits (pin levels; active-low pins read 0 when asserted)
WAVE_SIGNALS = ["AS20", "DS20", "RW20", "DSACK0", "DSACK1", "TA_BUS", "TS", "TIP",
                "INTCYCLE", "BGACK", "RESET", "BGDMA"]


def wave_addr(entry, k):
    return WAVE_BASE + k * WAVE_SLICE + entry


# Address log (0xB4 / 0xB5): one 8-byte entry per AS20 fall, a 1024-entry ring in the body
# region (only once the mailbox shows the body running), frozen when the waveform is done.
# Entry e at SPI LOG_BASE + 8*e: A31-A0 (BE); byte 4 SIZ1 SIZ0 RW20 region[2:0] 0 0;
# byte 5 + bit 7 of byte 6 = waveform write index of the run starting at that AS20 fall;
# byte 6 bits 6-0 + byte 7 = dt (fabric clks since the previous entry, saturating at 32767).
LOG_BASE = 0x0000
LOG_ENTRIES = 1024
LOG_ENTRY_BYTES = 8
LOG_DT_MAX = 0x7FFF
# 0xB5: magic, status, count u16, first u16, trigger u16 (bit 10 seen, bits 9-0 entry), next u16, lost u8
LOG_FRAME = [("status", 1), ("count", 2), ("first", 2), ("trigger", 2), ("next", 2), ("lost", 1)]
LOG_STATUS_ARMED, LOG_STATUS_FROZEN, LOG_STATUS_WRAPPED = 1, 2, 4
# 060 SIZ1/SIZ0: 00 long, 01 byte, 10 word, 11 line (16 bytes)
SIZ_NAMES = {0: "L", 1: "B", 2: "W", 3: "16"}


def log_entry(b):
    """One 8-byte address log entry -> dict."""
    return {"addr": int.from_bytes(b[0:4], "big"), "siz": b[4] >> 6, "rw": (b[4] >> 5) & 1,
            "region": (b[4] >> 2) & 7, "widx": b[5] | ((b[6] >> 7) << 8),
            "dt": ((b[6] & 0x7F) << 8) | b[7]}


def wave_duration(d):
    """dur[11] = 0: dur[10:0] clks; dur[11] = 1: dur[10:0] * 256 clks."""
    return (d & 0x7FF) << 8 if d & 0x800 else d


# ---- tprobe (0x80 read, 0x81 clear, 0xC0+n select, 0xE0+r region) -------------
TPROBE_READ, TPROBE_CLEAR, TPROBE_SEL, TPROBE_REGION = 0x80, 0x81, 0xC0, 0xE0
TPROBE_MAGIC = 0xC9
TPROBE_BINS = 32
TPROBE_SATURATE = 32768
# n: (name, shift, bin-0 meaning or None, phase measurement?)
MEAS = {
    0: ("TS fall -> AS20 fall", 0, None),
    1: ("INTCYCLE release -> AS20 fall", 0, None),
    2: ("AS20 low width", 0, None),
    3: ("DS20 fall -> DSACK fall (reads)", 2, None),
    4: ("DS20 fall -> DSACK fall (writes)", 2, None),
    5: ("AS20 rise -> DSACK rise", 0, "DSACK already high at AS rise"),
    6: ("DSACK fall -> last D change (reads)", 0, "no D change after DSACK fall"),
    7: ("DSACK fall -> TA_BUS fall", 0, None),
    8: ("TA_BUS fall -> AS20 rise", 0, None),
    9: ("AS20 rise -> next AS20 fall (idle)", 2, None),
    10: ("AS20 fall -> stale DSACK rise", 0, "DSACK high at AS fall (normal)"),
    11: ("INTCYCLE block -> AS20 rise (abandoned, AS <= 120 ns)", 0, "no block in this AS cycle"),
    12: ("DS20 rise -> DSACK rise", 0, "DSACK already high at DS rise"),
    13: ("TS fall -> TA_BUS fall (external cycles)", 2, None),
    14: ("write: AS20 fall -> last D change", 0, "no D change after AS fall"),
    15: ("write: DS20 fall -> last D change", 0, "no D change after DS fall"),
    16: ("phase: AS20 fall", None, None),
    17: ("phase: AS20 rise", None, None),
    18: ("phase: DS20 fall", None, None),
    19: ("phase: DSACK fall", None, None),
    20: ("phase: TS fall", None, None),
    21: ("phase: TA_BUS fall", None, None),
    22: ("phase: DSACK rise", None, None),
    23: ("phase: INTCYCLE release", None, None),
    24: ("phase: INTCYCLE block", None, None),
}
REGIONS = {"all": 0, "chip": 1, "cia": 2, "custom": 3, "rom": 4, "other": 5}
AMIGA_PERIOD_HALF = 8       # Amiga clock period in fabric half clocks (25 MHz at 100 MHz)

# ---- Pin readback image (diag_pins.bin, rtl/diag/pinread/diag_pins.v) ------------
# A CS-low transfer of PINREAD_LEN zero bytes returns PINREAD_MAGIC, then one bit
# per FPGA pin MSB first, in the order of PINREAD_ORDER (pin numbers as strings),
# sampled just before CS fell. SCK at most PINREAD_HZ (sampled at 25 MHz).
# 0xA2 answers with features bit 4 (pinread).
PINREAD_MAGIC = 0xA5
PINREAD_LEN = 14
PINREAD_HZ = 1_000_000
# pins diag_pins.bin drives (their bits read the pad back, not the board): RESET held low
# (keeps the 060 in reset through IC10), PCLK and CLKCPU 25 MHz, CLKEN low
# RESET, PCLK, CLKEN, CLKCPU; and the IDE strobes/selects parked high (IOR 116, IOW 115,
# IDECS 114/113: IOR is DIR of the IC4/IC5 data buffers, which must not drive D16-D31)
PINREAD_DRIVEN = {"117", "93", "85", "128", "116", "115", "114", "113"}
# pins the image holds high (IDE parked), with what a low readback means
PINREAD_PARKED_HIGH = {"116": "IOR", "115": "IOW", "114": "IDECS1", "113": "IDECS0"}
PINREAD_ORDER = [
    "105", "135", "39", "41", "45", "47", "48", "55", "56", "60", "62", "63", "104", "102",
    "101", "99", "98", "97", "96", "95", "94", "91", "90", "88", "87", "84", "82", "81",
    "80", "79", "76", "75", "74", "73", "106", "29", "31", "32", "33", "17", "16", "15",
    "12", "11", "10", "28", "9", "8", "138", "26", "25", "144", "4", "64", "112", "22",
    "1", "128", "85", "7", "134", "19", "34", "18", "3", "137", "139", "141", "130", "113",
    "114", "38", "52", "2", "142", "116", "115", "93", "24", "21", "20", "23", "117",
    "110", "71", "42", "43", "61", "143", "136", "78", "83", "49", "44", "125", "124",
    "107", "122", "121", "120", "119", "118",
]

# ---- CPLD JTAG (XC95288XL TQ144) ---------------------------------------------
JTAG_PINS = {"tck": 17, "tms": 4, "tdi": 22, "tdo": 27, "chip": 0}
IRLEN = 8
IDCODE_MASK = 0x0FFFFFFF    # version nibble ignored
IDCODE_VALUE = 0x09616093
IR = {"EXTEST": 0x00, "SAMPLE": 0x01, "USERCODE": 0xFD, "IDCODE": 0xFE, "BYPASS": 0xFF}
BSR_LEN = 864
BSR_FIELDS = 27             # openocd asserts on one 864-bit field: 27 x 32
