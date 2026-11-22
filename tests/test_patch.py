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
        self.assertEqual(len({p["id"] for p in patches}), len(patches))
        for p in patches:
            with self.subTest(p["id"]):
                for side in ("input", "output"):
                    self.assertRegex(p[side]["sha256"], r"^[0-9a-f]{64}$")
                self.assertEqual(p["input"]["size"], p["output"]["size"])
                self.assertTrue(p["suffix"].isalnum())
                self.assertIn("command", p["check"])
                if p["change"] is None:                  # nothing to change in the firmware bytes
                    self.assertEqual(p["edits"], [])
                    self.assertEqual(p["input"], p["output"])
                    continue
                offsets = [e["offset"] for e in p["edits"]]
                self.assertEqual(offsets, sorted(offsets))
                for e in p["edits"]:
                    self.assertEqual(len(e["from"]), len(e["to"]))
                    self.assertNotEqual(e["from"], e["to"])
                self.assertEqual(len(bytes.fromhex(p["change"]["before"])), 8)
                self.assertEqual(len(bytes.fromhex(p["change"]["after"])), 8)
                self.assertNotEqual(p["check"]["fixed"], p["check"]["original"])

    def test_speed_variants_are_distinct_and_consistent(self):
        patches = [p for p in patch.load_patches() if p["id"].startswith("alpha-40l")]
        self.assertEqual({p["variant"] for p in patches}, {"115200", "921600"})
        self.assertEqual(len({p["input"]["sha256"] for p in patches}), 1)
        self.assertEqual(len({p["output"]["sha256"] for p in patches}), len(patches))
        self.assertEqual(len({p["check"]["fixed"] for p in patches}), len(patches))
        self.assertEqual({p["module"]["speed_code"] for p in patches}, {"0x03", "0x11"})
        for p in patches:                                # the encoded immediate is really that speed
            mov, orr = struct.unpack("<II", bytes.fromhex(p["change"]["after"]))
            self.assertEqual(mov >> 20, 0xE3A)           # mov r0, #imm
            self.assertEqual(orr >> 20, 0xE38)           # orr r0, r0, #imm
            rot = lambda w: ((w & 0xFF) >> (2 * ((w >> 8) & 15)) | (w & 0xFF) << (32 - 2 * ((w >> 8) & 15))) & 0xFFFFFFFF
            self.assertEqual(rot(mov) | rot(orr), p["module"]["baud"])


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


class Variants(unittest.TestCase):
    """Same official file, one patch per module speed."""

    def setUp(self):
        base = {"model": "TSC Alpha-40L", "firmware": "M2.01.002", "official_file": "T.NEW",
                "input": {"size": 3, "sha256": hashlib.sha256(b"abc").hexdigest()}, "edits": [{"offset": 0}]}
        self.patches = [
            dict(base, id="a", variant="115200", output={"sha256": "1"}, module={"speed_code": "0x03"}),
            dict(base, id="b", variant="921600", output={"sha256": "2"}, module={"speed_code": "0x11"}),
            dict(base, id="c", variant=None, model="TSC Alpha-30L", input={"size": 3, "sha256": hashlib.sha256(b"xyz").hexdigest()},
                 output={"sha256": "3"}, module={"name": "xPico270"}),
        ]

    def test_default_and_explicit_speed(self):
        self.assertEqual(patch.find_patch(b"abc", self.patches)["id"], "a")
        self.assertEqual(patch.find_patch(b"abc", self.patches, "921600")["id"], "b")

    def test_model_without_variants_refuses_speed(self):
        self.assertEqual(patch.find_patch(b"xyz", self.patches)["id"], "c")
        with self.assertRaisesRegex(patch.PatchError, "no module-speed variants"):
            patch.find_patch(b"xyz", self.patches, "921600")

    def test_module_code_is_byte_1_of_the_eeprom_word(self):
        self.assertEqual(patch.module_code(0xBF0B1101), 0x11)
        self.assertEqual(patch.module_code(0xBF0B0301), 0x03)
        self.assertEqual(patch.module_code(3205173505), 0x11)   # the decimal answer of a real printer

    def test_variant_for_code(self):
        find = lambda code: patch.variant_for_code(self.patches, "Alpha-40L", "M2.01.002", code)
        self.assertEqual(find(0x03)["id"], "a")
        self.assertEqual(find(0x11)["id"], "b")
        self.assertIsNone(find(0x0B))

    def test_published_official_files_are_recognised(self):
        # the published variants of the real 40L file: the default stays the one that was published first
        real = [p for p in patch.load_patches() if p["id"].startswith("alpha-40l")]
        sha = real[0]["input"]["sha256"]
        self.assertEqual([p["id"] for p in patch.variants_for(sha, real)],
                         ["alpha-40l-m2.01.002", "alpha-40l-m2.01.002-921600"])


class Identify(unittest.TestCase):
    """A firmware file is recognised even behind the logo and Bluetooth-mode commands."""

    def setUp(self):
        self.official = tscfw.pack(b"FuseA40L", 0x100000, 4096, tscfw.emit([("L", b, 0) for b in b"\x00" * 256]))
        self.patches = [{"id": "x", "model": "TSC Alpha-40L", "firmware": "M0",
                         "input": {"sha256": hashlib.sha256(self.official).hexdigest()},
                         "output": {"sha256": "0" * 64}, "edits": [{"offset": 0}]}]

    def test_with_and_without_our_commands(self):
        for data in (self.official, patch.build(self.official, dual=True, logo=bmp(8, 8))):
            self.assertEqual(patch.identify(data, self.patches)[1], "official")

    def test_unknown_and_not_a_firmware(self):
        self.assertIsNone(patch.identify(self.official + b"tail", self.patches))
        self.assertIsNone(patch.firmware_part(bmp(8, 8)))
        self.assertIsNone(patch.identify(bmp(8, 8), self.patches))


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

    def test_30l_logo_stays_above_the_ip_line(self):
        rows = next(p["logo_rows"] for p in patch.load_patches() if p["model"].endswith("Alpha-30L"))
        own, usual = (ROOT / "docs/assets/marker-logo-30l.bmp").read_bytes(), (ROOT / "docs/assets/marker-logo.bmp").read_bytes()
        self.assertEqual(patch.check_bmp(own), (320, 138))
        self.assertFalse(patch.covered_rows(own, rows))
        self.assertTrue(patch.covered_rows(usual, rows))     # the Alpha-40L picture would be covered

    def test_cli_speed_and_no_change_model(self):
        original = tscfw.pack(b"FuseA40L", 0x100000, 4096, tscfw.emit([("L", b, 0) for b in b"\x00" * 256]))
        sha = hashlib.sha256(original).hexdigest()
        base = {"model": "TSC Alpha-30L", "firmware": "M0", "official_file": "T.NEW", "suffix": "SETUP", "logo": True,
                "input": {"size": len(original), "sha256": sha}, "output": {"size": len(original), "sha256": sha},
                "edits": [], "change": None, "check": {"command": "OUT", "fixed": "Dual", "original": "BT4.0"}}
        saved, patch.load_patches = patch.load_patches, lambda: [base]
        try:
            with tempfile.TemporaryDirectory() as d:
                src = Path(d, "FW.NEW"); src.write_bytes(original)
                self.assertEqual(patch.main([str(src)]), 1)            # nothing to build without options
                self.assertEqual(patch.main([str(src), "--speed", "921600"]), 1)
                self.assertEqual(patch.main([str(src), "--dual"]), 0)
                self.assertEqual(Path(d, "FW_SETUP.NEW").read_bytes(), b"BT MODE Dual\r\n" + original)
        finally:
            patch.load_patches = saved

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
