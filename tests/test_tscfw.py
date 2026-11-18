"""Format library: checksums, LZSS round trip, in-place literal change.

The real firmware is TSC's and is not part of this repository, so these tests
build small synthetic firmware files with the same layout.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import tscfw  # noqa: E402


def literal_stream(data: bytes) -> bytes:
    """LZSS stream made of literals only (flag byte 0xFF per 8 bytes)."""
    return tscfw.emit([("L", b, 0) for b in data])


class FormatTest(unittest.TestCase):
    def make(self, image: bytes, area: int = 4096) -> bytes:
        return tscfw.pack(b"FuseA40L", 0x100000, area, literal_stream(image))

    def test_pack_then_parse_round_trip(self):
        image = bytes(range(256)) * 3
        raw = self.make(image)
        self.assertTrue(raw.startswith(b"FuseA40L 00100000,00001000,"))
        fw = tscfw.parse(raw)
        self.assertEqual(fw.image, image)
        self.assertEqual(tscfw.pack(fw.name, fw.address, fw.area, fw.stream), raw)

    def test_checksums_are_enforced(self):
        raw = bytearray(self.make(b"hello firmware"))
        raw[-1] ^= 1  # padding byte: both sums break
        with self.assertRaises(tscfw.FirmwareError):
            tscfw.parse(bytes(raw))

    def test_match_tokens_unpack_from_the_space_filled_window(self):
        # One match of length 3 at window position 0 copies three 0x20 bytes.
        tokens = [("L", 0x41, 0), ("M", 0, 3), ("L", 0x42, 0)]
        self.assertEqual(tscfw.unpack(tokens), b"A   B")
        stream = tscfw.emit(tokens)
        self.assertEqual(tscfw.tokenize(stream)[0], tokens)

    def test_stray_tail_byte_is_kept(self):
        # Vendor streams end with a flag byte whose next token would be a
        # match, but only one byte follows: decoders stop there.
        stream = tscfw.emit([("L", 0x41, 0)] * 7 + [("M", 4078, 3)]) + b"\x00\xe9"
        tokens, tail = tscfw.tokenize(stream)
        self.assertEqual(len(tokens), 8)
        self.assertEqual(tail, b"\xe9")

    def test_replace_in_place_changes_only_literals(self):
        image = b"\x00" * 64 + bytes.fromhex("f40aa0e3900f80e3") + b"\x11" * 64
        fw = tscfw.parse(self.make(image))
        fixed = tscfw.parse(tscfw.replace_in_place(
            fw, 64, bytes.fromhex("f40aa0e3900f80e3"), bytes.fromhex("0709a0e3800f80e3")))
        self.assertEqual(tscfw.image_diff(fw, fixed), [64, 65, 68])
        self.assertEqual(len(fixed.stream), len(fw.stream))

    def test_replace_in_place_refuses_a_change_inside_a_match(self):
        fw = tscfw.parse(tscfw.pack(b"FuseA40L", 0x100000, 4096, tscfw.emit([("M", 0, 3)])))
        with self.assertRaises(tscfw.FirmwareError):
            tscfw.replace_in_place(fw, 0, b"   ", b"abc")


if __name__ == "__main__":
    unittest.main()
