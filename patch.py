#!/usr/bin/env python3
"""Build a fixed firmware file for a TSC Alpha printer from TSC's official one.

    python3 patch.py ALPHA40L_EZC_M2.01.002.NEW
    python3 patch.py ALPHA40L_EZC_M2.01.002.NEW --dual --logo logo.bmp
    python3 patch.py --logo logo.bmp -o LOGO.prn          (logo only, no firmware)
    python3 patch.py ALPHA40L_EZC_M2.01.002.NEW --verify  (show what changes, then build)

Standard library only (Python 3.8+). Nothing is downloaded: the official file
is checked by SHA-256 against docs/patches/*.json, a handful of bytes are
replaced, and the result is checked by SHA-256 again.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PATCHES = ROOT / "docs" / "patches"

DUAL = b"BT MODE Dual\r\n"
LOGO_MAX_W, LOGO_MAX_H = 320, 140


class PatchError(Exception):
    pass


def load_patches() -> list:
    index = json.loads((PATCHES / "index.json").read_text(encoding="utf-8"))
    return [json.loads((PATCHES / name).read_text(encoding="utf-8")) for name in index["patches"]]


def find_patch(data: bytes, patches: list) -> dict:
    digest = hashlib.sha256(data).hexdigest()
    for p in patches:
        if p["input"]["sha256"] == digest:
            return p
        if p["output"]["sha256"] == digest:
            raise PatchError(f"this file is already fixed ({p['model']} {p['firmware']})")
    known = "\n".join(f"  {p['official_file']}  {p['input']['sha256']}" for p in patches)
    raise PatchError(f"unknown file (sha256 {digest}).\nSupported official files:\n{known}")


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
    ap.add_argument("-o", "--output", help="output file (default: <input>_BTFIX.NEW)")
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
        patch = find_patch(original, load_patches())
        fixed = apply_patch(original, patch)
        print(f"{patch['model']} {patch['firmware']}: official file recognised")
        if patch.get("logo") is not True and logo:
            raise PatchError("the logo option is not tested on this model")
        if args.verify:
            describe(original, fixed, patch)
        out = Path(args.output) if args.output else src.with_name(f"{src.stem}_BTFIX.NEW")
        data = build(fixed, args.dual, logo)
        out.write_bytes(data)
        extras = [x for x, on in (("logo", logo), ("Dual mode", args.dual)) if on]
        print(f"written {out} ({len(data):,} bytes){' with ' + ' + '.join(extras) if extras else ''}")
        print(f"  firmware sha256 {patch['output']['sha256']}")
        print("Send it to the printer like the official file (DiagTool > Download, or the driver's")
        print("'Send File To Printer'). The printer restarts by itself. Check with:")
        print(f"  {patch['check']['command']}  ->  {patch['check']['fixed']}")
        return 0
    except (PatchError, OSError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
