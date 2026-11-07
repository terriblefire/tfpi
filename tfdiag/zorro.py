"""Expansion boards found by the exerciser (body version 3, tests 0x04-0x06).

The exerciser walks the autoconfig chain once per boot, before the first preset,
the way expansion.library does (Zorro III config space $FF000000, then Zorro II
$E80000), records each board, and shuts it up (or configures it if it cannot be
shut up) so the next one appears. Kickstart never runs, so this is the chain as
the hardware presents it. The TF card's own autoconfig is off in the diag image
(the A314 interface Kickstart normally lists is the TF's, and is absent here).

Entries (protocol: docs/DIAGNOSTICS.md in the diag firmware):
  0x04 board      size = index | 0x80 (Zorro III space), addr = serial,
                  expected = type<<24 | product<<16 | flags<<8 | rsvd03,
                  got = manufacturer<<16 | InitDiagVec
  0x05 configured size = index, addr = base given, expected = bytes, got = config space
  0x06 chain end  size = boards, addr = reason, expected/got = Z3/Z2 config reads with a bus error
"""

BOARD, CONFIGURED, END = 0x04, 0x05, 0x06
END_REASONS = {0: "no more boards", 1: "a board did not go away when shut up or configured",
               2: "16-board limit reached", 3: "no address room to configure a board that cannot be shut up"}

# Manufacturer IDs (hints for the report only; the raw numbers are always shown)
MANUFACTURERS = {
    513: "Commodore (Braunschweig)", 514: "Commodore (West Chester)", 2017: "GVP",
    2167: "Village Tronic", 4626: "Individual Computers", 8512: "Phase 5", 18260: "MacroSystem",
    28014: "MNT", 5080: "A314",
}
Z2_SIZES = {0: 8 << 20, 1: 64 << 10, 2: 128 << 10, 3: 256 << 10, 4: 512 << 10, 5: 1 << 20, 6: 2 << 20, 7: 4 << 20}


def _i(v):
    return int(v, 16) if isinstance(v, str) else int(v)


def size_text(n):
    if n is None:
        return "?"
    if n >= 1 << 20:
        return f"{n >> 20} MB"
    return f"{n >> 10} KB"


def board_size(zorro3, er_type, er_flags):
    code = er_type & 7
    if zorro3:
        if er_flags & 0x20 and code != 7:      # ERFF_EXTENDED: 16 MB << code
            return (16 << 20) << code
        return 16 << 20
    return Z2_SIZES[code]


def decode(results):
    """One board list from the result entries. After a hang the exerciser runs again and
    lists the boards again: the first complete list (ending in 0x06) is used."""
    runs, cur = [], None
    for r in results:
        t = r.get("test")
        if t not in (BOARD, CONFIGURED, END) or "info" not in r.get("flags", []):
            continue
        idx = r.get("size", 0) & 0x0F
        if t == BOARD and (cur is None or cur.get("end") is not None or idx == 0):
            cur = {"boards": [], "end": None}
            runs.append(cur)
        if cur is None:
            cur = {"boards": [], "end": None}
            runs.append(cur)
        if t == BOARD:
            exp, got = _i(r["expected"]), _i(r["got"])
            z3 = bool(r.get("size", 0) & 0x80)
            er_type, product, flags = exp >> 24, (exp >> 16) & 0xFF, (exp >> 8) & 0xFF
            man = got >> 16
            cur["boards"].append({
                "index": idx, "zorro": "III" if z3 else "II", "manufacturer": man,
                "manufacturer_name": MANUFACTURERS.get(man, ""), "product": product,
                "serial": f"0x{_i(r['addr']):08x}", "er_type": f"0x{er_type:02x}", "er_flags": f"0x{flags:02x}",
                "size": board_size(z3, er_type, flags), "memory": bool(er_type & 0x20),
                "boot_rom": bool(er_type & 0x10), "chained": bool(er_type & 0x08),
                "no_shutup": bool(flags & 0x40), "diag_vec": f"0x{got & 0xFFFF:04x}", "configured_at": None})
        elif t == CONFIGURED:
            for b in cur["boards"]:
                if b["index"] == idx:
                    b["configured_at"] = f"0x{_i(r['addr']):08x}"
        else:
            reason = _i(r["addr"])
            cur["end"] = {"count": r.get("size", 0), "reason": reason,
                          "reason_text": END_REASONS.get(reason, f"reason {reason}"),
                          "z3_bus_errors": _i(r["expected"]), "z2_bus_errors": _i(r["got"])}
    if not runs:
        return None
    done = [x for x in runs if x["end"] is not None]
    pick = done[0] if done else runs[0]
    pick["passes"] = len(runs)
    return pick


def describe(b):
    name = b["manufacturer_name"] or "manufacturer"
    kind = "memory" if b["memory"] else "I/O"
    extra = []
    if b["boot_rom"]:
        extra.append("boot ROM")
    if b["no_shutup"]:
        extra.append("cannot be shut up")
    if b["configured_at"]:
        extra.append(f"configured at {b['configured_at']} for the walk")
    return (f"Zorro {b['zorro']} {kind}, {size_text(b['size'])}: {name} {b['manufacturer']} "
            f"(0x{b['manufacturer']:04x}) product {b['product']}, serial {b['serial']}"
            + (f" ({', '.join(extra)})" if extra else ""))
