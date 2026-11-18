"""patch.py: patch definitions, byte edits, logo and Dual-mode prefixes."""
import hashlib
import json
import struct
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import patch  # noqa: E402
import tscfw  # noqa: E402


def bmp(width: int, height: int, bpp: int = 24) -> bytes:
    row = (width * 3 + 3) & ~3
    size = 54 + row * height
    header = struct.pack("<2sIHHI", b"BM", size, 0, 0, 54)
    info = struct.pack("<IiiHHIIiiII", 40, width, height, 1, bpp, 0, row * height, 2835, 2835, 0, 0)
    return header + info + bytes(row * height)


class Definitions(unittest.TestCase):
    """Every published patch must be internally consistent."""

    def test_patch_files(self):
        patches = patch.load_patches()
        self.assertTrue(patches)
        for p in patches:
            with self.subTest(p["id"]):
                for side in ("input", "output"):
                    self.assertRegex(p[side]["sha256"], r"^[0-9a-f]{64}$")
                self.assertEqual(p["input"]["size"], p["output"]["size"])
                offsets = [e["offset"] for e in p["edits"]]
                self.assertEqual(offsets, sorted(offsets))
                for e in p["edits"]:
                    self.assertEqual(len(e["from"]), len(e["to"]))
                    self.assertNotEqual(e["from"], e["to"])
                self.assertEqual(len(bytes.fromhex(p["change"]["before"])), 8)


class Apply(unittest.TestCase):
    def setUp(self):
        image = b"\x00" * 64 + bytes.fromhex("f40aa0e3900f80e3") + b"\x22" * 64
        self.original = tscfw.pack(b"FuseA40L", 0x100000, 4096, tscfw.emit([("L", b, 0) for b in image]))
        fw = tscfw.parse(self.original)
        self.fixed = tscfw.replace_in_place(
            fw, 64, bytes.fromhex("f40aa0e3900f80e3"), bytes.fromhex("0709a0e3800f80e3"))
        edits, i = [], 0
        while i < len(self.original):
            if self.original[i] != self.fixed[i]:
                j = i
                while self.original[j] != self.fixed[j]:
                    j += 1
                edits.append({"offset": i, "from": self.original[i:j].hex(), "to": self.fixed[i:j].hex()})
                i = j
            else:
                i += 1
        self.patch = {
            "id": "synthetic", "model": "Test", "firmware": "M0", "official_file": "TEST.NEW",
            "input": {"size": len(self.original), "sha256": hashlib.sha256(self.original).hexdigest()},
            "output": {"size": len(self.fixed), "sha256": hashlib.sha256(self.fixed).hexdigest()},
            "edits": edits,
        }

    def test_apply_gives_the_tested_file(self):
        self.assertEqual(patch.apply_patch(self.original, self.patch), self.fixed)

    def test_recognises_official_and_already_fixed_files(self):
        self.assertIs(patch.find_patch(self.original, [self.patch]), self.patch)
        with self.assertRaisesRegex(patch.PatchError, "already fixed"):
            patch.find_patch(self.fixed, [self.patch])
        with self.assertRaisesRegex(patch.PatchError, "unknown file"):
            patch.find_patch(b"something else", [self.patch])

    def test_wrong_bytes_are_refused(self):
        broken = bytearray(self.original)
        broken[self.patch["edits"][-1]["offset"]] ^= 0xFF
        with self.assertRaises(patch.PatchError):
            patch.apply_patch(bytes(broken), self.patch)


class Extras(unittest.TestCase):
    def test_logo_rules(self):
        self.assertEqual(patch.check_bmp(bmp(320, 138)), (320, 138))
        for bad in (bmp(321, 10), bmp(10, 141), bmp(10, 10, bpp=32), b"PNG"):
            with self.assertRaises(patch.PatchError):
                patch.check_bmp(bad)

    def test_build_order_logo_then_dual_then_firmware(self):
        logo = bmp(4, 2)
        out = patch.build(b"FIRMWARE", dual=True, logo=logo)
        self.assertTrue(out.startswith(b'\r\nDOWNLOAD F,"LOGO.LCD",%d,' % len(logo) + logo))
        self.assertIn(b'COPY F,"LOGO.LCD",S,"LOGO.LCD"\r\nKILL F,"LOGO.LCD"\r\nBT MODE Dual\r\nFIRMWARE', out)
        self.assertTrue(out.endswith(b"FIRMWARE"))

    def test_marker_logo_fits_the_screen(self):
        self.assertEqual(patch.check_bmp((ROOT / "docs/assets/marker-logo.bmp").read_bytes()), (320, 138))

    def test_cli_logo_only(self):
        with tempfile.TemporaryDirectory() as d:
            src, out = Path(d, "logo.bmp"), Path(d, "LOGO.prn")
            src.write_bytes(bmp(8, 8))
            self.assertEqual(patch.main(["--logo", str(src), "-o", str(out)]), 0)
            self.assertEqual(out.read_bytes(), patch.logo_commands(bmp(8, 8)))

    def test_autobas_checks_model_and_firmware_first(self):
        text = (ROOT / "tools/AUTO.BAS").read_bytes()
        self.assertNotIn(b"\n", text.replace(b"\r\n", b""))  # CRLF only, as the printer expects
        lines = text.decode().split("\r\n")
        first_if = next(i for i, l in enumerate(lines) if l.startswith("IF"))
        self.assertIn('"MODEL")="Alpha-40L"', lines[first_if])
        self.assertIn('"VERSION")="2.01.002"', lines[first_if + 1])
        self.assertTrue(all(l.startswith(("REM", "IF", "POKE32", "DELAY", "ENDIF")) for l in lines if l))


if __name__ == "__main__":
    unittest.main()
