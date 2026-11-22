#!/usr/bin/env python3
"""Build a fixed firmware file for a TSC Alpha printer from TSC's official one.

    python3 patch.py ALPHA40L_EZC_M2.01.002.NEW                    (module at 115200, code 03)
    python3 patch.py ALPHA40L_EZC_M2.01.002.NEW --speed 921600     (module at 921600, code 11)
    python3 patch.py ALPHA40L_EZC_M2.01.002.NEW --dual --logo logo.bmp
    python3 patch.py ALPHA30L_EZC_M2.01.002.NEW --dual --logo docs/assets/marker-logo-30l.bmp
    python3 patch.py --logo logo.bmp -o LOGO.prn          (logo only, no firmware)
    python3 patch.py ALPHA40L_EZC_M2.01.002.NEW --verify  (show what changes, then build)

Which --speed? The Bluetooth module of the Alpha-40L keeps its own UART speed, and it differs
between printers of the same model. Read it with `python3 tools/tsc_usb.py info`.

Standard library only (Python 3.8+). Nothing is downloaded: the official file
is checked by SHA-256 against docs/patches/*.json, a handful of bytes are
replaced, and the result is checked by SHA-256 again.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PATCHES = ROOT / "docs" / "patches"

DUAL = b"BT MODE Dual\r\n"
LOGO_MAX_W, LOGO_MAX_H = 320, 140
# Background of the printer screen (RGB 32,24,16 = 0x20C2 in RGB565).
SCREEN_BG = (32, 24, 16)
DEFAULT_SPEED = "115200"


class PatchError(Exception):
    pass


def load_patches() -> list:
    index = json.loads((PATCHES / "index.json").read_text(encoding="utf-8"))
    return [json.loads((PATCHES / name).read_text(encoding="utf-8")) for name in index["patches"]]


def variants_for(digest: str, patches: list) -> list:
    """Every published patch for this official file (one per module speed)."""
    return [p for p in patches if p["input"]["sha256"] == digest]


def find_patch(data: bytes, patches: list, speed: str | None = None) -> dict:
    digest = hashlib.sha256(data).hexdigest()
    hits = variants_for(digest, patches)
    if not hits:
        for p in patches:
            if p["edits"] and p["output"]["sha256"] == digest:
                name = f"{p['model']} {p['firmware']}" + (f", {p['variant']} variant" if p.get("variant") else "")
                raise PatchError(f"this file is already fixed ({name})")
        known = "\n".join(f"  {p['official_file']}  {p['input']['sha256']}"
                          for p in {q["input"]["sha256"]: q for q in patches}.values())
        raise PatchError(f"unknown file (sha256 {digest}).\nSupported official files:\n{known}")
    if len(hits) == 1 and not hits[0].get("variant"):
        if speed:
            raise PatchError(f"{hits[0]['model']} has no module-speed variants; leave out --speed")
        return hits[0]
    wanted = speed or DEFAULT_SPEED
    for p in hits:
        if p["variant"] == wanted:
            return p
    raise PatchError(f"no {wanted} variant; available: " + ", ".join(p["variant"] for p in hits))


def module_code(word: int) -> int:
    """Speed code of the BM78 module: byte 1 of the EEPROM word at 0x30 (PEEK32 of the RAM copy)."""
    return (word >> 8) & 0xFF


def variant_for_code(patches: list, model: str, firmware: str, code: int) -> dict | None:
    for p in patches:
        if p["model"].endswith(model) and p["firmware"].startswith(firmware) and "speed_code" in p.get("module", {}):
            if int(p["module"]["speed_code"], 16) == code:
                return p
    return None


def firmware_part(data: bytes) -> bytes | None:
    """The firmware inside a file that may start with logo and Bluetooth-mode commands."""
    for m in re.finditer(rb"FuseA\w{3} [0-9A-F]{8},[0-9A-F]{8},[0-9A-F]{8},", data):
        return data[m.start():]
    return None


def identify(data: bytes, patches: list) -> tuple | None:
    """-> (patch, 'official' | 'fixed') for a known firmware file (with or without our commands), else None."""
    part = firmware_part(data)
    if part is None:
        return None
    digest = hashlib.sha256(part).hexdigest()
    for p in patches:
        if p["input"]["sha256"] == digest:
            return p, "official"
        if p["output"]["sha256"] == digest:
            return p, "fixed"
    return None


def apply_patch(data: bytes, patch: dict) -> bytes:
    if len(data) != patch["input"]["size"]:
        raise PatchError("file size does not match the official file")
    out = bytearray(data)
    for e in patch["edits"]:
        old, new, at = bytes.fromhex(e["from"]), bytes.fromhex(e["to"]), e["offset"]
        if out[at:at + len(old)] != old:
            raise PatchError(f"unexpected bytes at offset {at}")
        out[at:at + len(new)] = new
    if hashlib.sha256(out).hexdigest() != patch["output"]["sha256"]:
        raise PatchError("result does not match the tested file (sha256)")
    return bytes(out)


def covered_rows(bmp: bytes, rows: int) -> bool:
    """True if the logo draws something below the first ``rows`` rows (where the Alpha-30L shows its IP address)."""
    width, height = check_bmp(bmp)
    pad = (width * 3 + 3) & ~3
    for y in range(rows, height):
        line = bmp[54 + (height - 1 - y) * pad:][:width * 3]
        for x in range(0, width * 3, 3):
            b, g, r = line[x:x + 3]
            if max(abs(r - SCREEN_BG[0]), abs(g - SCREEN_BG[1]), abs(b - SCREEN_BG[2])) > 12:
                return True
    return False


def check_bmp(bmp: bytes) -> tuple:
    """-> (width, height) of a logo the printer can show, or PatchError."""
    if len(bmp) < 54 or bmp[:2] != b"BM":
        raise PatchError("logo must be a .bmp file")
    offset, header, width, height, _planes, bpp, compression = struct.unpack_from("<IIiiHHI", bmp, 10)
    if offset != 54 or header != 40 or bpp != 24 or compression != 0:
        raise PatchError("logo must be a plain 24-bit BMP (54-byte header, no compression)")
    if not (0 < width <= LOGO_MAX_W and 0 < height <= LOGO_MAX_H):
        raise PatchError(f"logo is {width}x{height}; it must fit {LOGO_MAX_W}x{LOGO_MAX_H} (bottom-up rows)")
    if len(bmp) < 54 + ((width * 3 + 3) & ~3) * height:
        raise PatchError("logo file is truncated")
    return width, height


def logo_commands(bmp: bytes) -> bytes:
    """TSPL that stores the BMP as /system/LOGO.LCD (DOWNLOAD refuses that drive, COPY does not)."""
    check_bmp(bmp)
    return (b'\r\nDOWNLOAD F,"LOGO.LCD",%d,' % len(bmp) + bmp
            + b'\r\nCOPY F,"LOGO.LCD",S,"LOGO.LCD"\r\nKILL F,"LOGO.LCD"\r\n')


def build(firmware: bytes | None, dual: bool = False, logo: bytes | None = None) -> bytes:
    """Logo commands, then Bluetooth mode, then the firmware - the order that was tested."""
    return (logo_commands(logo) if logo else b"") + (DUAL if dual else b"") + (firmware or b"")


def describe(original: bytes, fixed: bytes, patch: dict) -> None:
    """Independent check: unpack both files and list every changed byte of the firmware image."""
    sys.path.insert(0, str(ROOT))
    import tscfw

    if patch["change"] is None:
        print("  this model needs no change in the firmware bytes")
        return
    a, b = tscfw.parse(original), tscfw.parse(fixed)
    changed = tscfw.image_diff(a, b)
    print(f"  firmware image: {len(a.image):,} bytes; changed: {len(changed)}")
    for i in changed:
        print(f"    {tscfw.IMAGE_BASE + i:#010x}: {a.image[i]:02x} -> {b.image[i]:02x}")
    c = patch["change"]
    print(f"  {c['function']} @ {c['address']}")
    print(f"    before: {c['asm_before']}")
    print(f"    after:  {c['asm_after']}")
    print("  both checksums are valid; the file length and structure are unchanged")


def main(argv: list | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("firmware", nargs="?", help="official firmware file from TSC (*.NEW)")
    ap.add_argument("-o", "--output", help="output file (default: <input>_BTFIX.NEW or similar)")
    ap.add_argument("--speed", choices=("115200", "921600"),
                    help="UART speed of the Bluetooth module, see `tools/tsc_usb.py info` (default: 115200)")
    ap.add_argument("--dual", action="store_true", help="also switch Bluetooth to Dual mode (Classic + LE)")
    ap.add_argument("--logo", metavar="BMP", help="also install a home-screen logo (24-bit BMP, up to 320x140)")
    ap.add_argument("--verify", action="store_true", help="unpack both files and show every changed byte")
    args = ap.parse_args(argv)

    try:
        logo = Path(args.logo).read_bytes() if args.logo else None
        if logo:
            check_bmp(logo)
        if args.firmware is None:
            if not logo or not args.output:
                ap.error("give a firmware file, or --logo BMP with -o OUTPUT")
            Path(args.output).write_bytes(build(None, args.dual, logo))
            print(f"written {args.output}: logo{' + Dual mode' if args.dual else ''}, no firmware")
            return 0

        src = Path(args.firmware)
        original = src.read_bytes()
        patch = find_patch(original, load_patches(), args.speed)
        fixed = apply_patch(original, patch)
        variant = f", module speed {patch['variant']}" if patch.get("variant") else ""
        print(f"{patch['model']} {patch['firmware']}: official file recognised{variant}")
        if not patch["edits"] and not (args.dual or logo):
            raise PatchError("this model needs no change in the firmware; add --dual and/or --logo")
        if patch.get("logo") is not True and logo:
            raise PatchError("the logo option is not tested on this model")
        if logo and patch.get("logo_rows") and covered_rows(logo, patch["logo_rows"]):
            print(f"warning: on this model the IP address line covers the logo below row {patch['logo_rows']}; "
                  f"use {patch['logo_file'].replace('assets/', 'docs/assets/')} or move the picture up", file=sys.stderr)
        if args.verify:
            describe(original, fixed, patch)
        out = Path(args.output) if args.output else src.with_name(f"{src.stem}_{patch.get('suffix', 'BTFIX')}.NEW")
        data = build(fixed, args.dual, logo)
        out.write_bytes(data)
        extras = [x for x, on in (("logo", logo), ("Dual mode", args.dual)) if on]
        print(f"written {out} ({len(data):,} bytes){' with ' + ' + '.join(extras) if extras else ''}")
        print(f"  firmware sha256 {patch['output']['sha256']}")
        print("Send it to the printer like the official file (DiagTool > Download, or the driver's")
        print("'Send File To Printer'). The printer restarts by itself. Check with:")
        print(f"  {patch['check']['command']}  ->  {patch['check']['fixed']}")
        print("After flashing the Bluetooth mode may be back to BT4.0 (LE only): check it and run")
        print("  python3 tools/tsc_usb.py btmode Dual   if you need Bluetooth Classic")
        return 0
    except (PatchError, OSError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
