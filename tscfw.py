"""Read, check and rebuild TSC Alpha firmware update files (``*.NEW``).

Standard library only. Used by ``patch.py --verify`` and by the tests; you do
not need it to apply a patch.

File layout (TSC Alpha-40L / Alpha-30L, firmware M2.x)::

    b"FuseA40L 00100000,00370000,19E9F842,"   ASCII command: flash address,
                                               payload length, payload byte-sum
    payload (length bytes):
        u32  byte-sum of everything after these 8 bytes
        u32  n = length of the data that follows
        n    b"TS01" + LZSS stream
        ...  zero padding up to the payload length

The printer checks the two byte-sums, unpacks the stream and writes the image
to NAND at the flash address. There is no signature.

LZSS is the classic Okumura variant: 4096-byte window pre-filled with 0x20,
first write position 4078, one flag byte per 8 tokens (bit 0 first,
1 = literal byte, 0 = 2-byte match: position low byte, position high nibble
and length - 3 in the low nibble, lengths 3..18). Vendor streams end with one
stray byte after the last complete token; it is kept as ``tail``.
"""
from __future__ import annotations

import hashlib
import struct
from dataclasses import dataclass
from typing import List, Tuple

WINDOW = 4096
MAX_MATCH = 18
IMAGE_BASE = 0x20000000  # where the firmware runs; image offset 0 = this address

Token = Tuple[str, int, int]  # ("L", byte, 0) or ("M", window position, length)


class FirmwareError(ValueError):
    """The file is not a firmware update file this tool understands."""


@dataclass
class Firmware:
    name: bytes        # b"FuseA40L"
    address: int       # flash address
    area: int          # payload length
    payload_sum: int
    data_sum: int
    data_length: int
    stream: bytes      # LZSS stream (without b"TS01")
    tokens: List[Token]
    tail: bytes
    image: bytes       # unpacked firmware, runs at IMAGE_BASE

    def address_of(self, offset: int) -> int:
        return IMAGE_BASE + offset


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def split_header(raw: bytes) -> Tuple[bytes, int, int, int, bytes]:
    """-> (name, address, area, payload_sum, payload)."""
    try:
        end = 0
        for _ in range(3):
            end = raw.index(b",", end) + 1
        name, numbers = raw[:end].split(b" ", 1)
        address, area, payload_sum = (int(x, 16) for x in numbers.rstrip(b",").split(b","))
    except ValueError as e:
        raise FirmwareError("no 'FuseXXXX addr,len,sum,' header") from e
    if not name.startswith(b"Fuse"):
        raise FirmwareError(f"unexpected header {name!r}")
    return name, address, area, payload_sum, raw[end:]


def tokenize(stream: bytes) -> Tuple[List[Token], bytes]:
    tokens: List[Token] = []
    i, n = 0, len(stream)
    while i < n:
        flags = stream[i]
        i += 1
        for bit in range(8):
            if i >= n:
                break
            if flags >> bit & 1:
                tokens.append(("L", stream[i], 0))
                i += 1
            else:
                if i + 1 >= n:
                    if flags >> bit:
                        raise FirmwareError("flag bits after the last token")
                    return tokens, stream[i:]
                lo, hi = stream[i], stream[i + 1]
                i += 2
                tokens.append(("M", lo | (hi & 0xF0) << 4, (hi & 0x0F) + 3))
    return tokens, b""


def emit(tokens: List[Token]) -> bytes:
    out = bytearray()
    for g in range(0, len(tokens), 8):
        flags, body = 0, bytearray()
        for bit, (kind, a, b) in enumerate(tokens[g:g + 8]):
            if kind == "L":
                flags |= 1 << bit
                body.append(a)
            else:
                body += bytes((a & 0xFF, (a >> 4 & 0xF0) | (b - 3)))
        out.append(flags)
        out += body
    return bytes(out)


def unpack(tokens: List[Token]) -> bytes:
    ring = bytearray(b"\x20" * WINDOW)
    r = WINDOW - MAX_MATCH
    out = bytearray()
    for kind, a, b in tokens:
        if kind == "L":
            out.append(a)
            ring[r] = a
            r = (r + 1) & (WINDOW - 1)
        else:
            for i in range(b):
                c = ring[(a + i) & (WINDOW - 1)]
                out.append(c)
                ring[r] = c
                r = (r + 1) & (WINDOW - 1)
    return bytes(out)


def parse(raw: bytes) -> Firmware:
    """Parse and check a firmware file; raises FirmwareError on any mismatch."""
    name, address, area, payload_sum, payload = split_header(raw)
    if len(payload) != area:
        raise FirmwareError(f"payload is {len(payload)} bytes, header says {area}")
    if sum(payload) & 0xFFFFFFFF != payload_sum:
        raise FirmwareError("payload checksum mismatch")
    data_sum, n = struct.unpack_from("<II", payload, 0)
    if sum(payload[8:]) & 0xFFFFFFFF != data_sum:
        raise FirmwareError("data checksum mismatch")
    if payload[8:12] != b"TS01":
        raise FirmwareError("no TS01 marker")
    if any(payload[8 + n:]):
        raise FirmwareError("padding is not zero")
    stream = payload[12:8 + n]
    tokens, tail = tokenize(stream)
    if emit(tokens) + tail != stream:
        raise FirmwareError("stream does not re-encode identically")
    return Firmware(name, address, area, payload_sum, data_sum, n, stream, tokens, tail, unpack(tokens))


def pack(name: bytes, address: int, area: int, stream: bytes) -> bytes:
    """Build a firmware file around an LZSS stream (both checksums computed)."""
    body = b"TS01" + stream
    if len(body) + 8 > area:
        raise FirmwareError("stream does not fit the firmware area")
    rest = body + bytes(area - 8 - len(body))
    payload = struct.pack("<II", sum(rest) & 0xFFFFFFFF, len(body)) + rest
    return name + b" %08X,%08X,%08X," % (address, area, sum(payload) & 0xFFFFFFFF) + payload


def replace_in_place(fw: Firmware, offset: int, old: bytes, new: bytes) -> bytes:
    """Rebuild the file with ``new`` at image ``offset`` without moving any token.

    Only literal tokens may change: if a changed byte sits inside a match, or a
    later match copies a changed byte, this raises - choose another encoding of
    the instruction instead. This keeps the result as close to the vendor file
    as possible (same length, same structure).
    """
    if fw.image[offset:offset + len(old)] != old:
        raise FirmwareError(f"unexpected bytes at {fw.address_of(offset):#x}")
    image = bytearray(fw.image)
    image[offset:offset + len(new)] = new
    tokens: List[Token] = []
    pos = 0
    for kind, a, b in fw.tokens:
        if kind == "L":
            tokens.append(("L", image[pos], 0))
            pos += 1
        else:
            tokens.append((kind, a, b))
            pos += b
    stream = emit(tokens) + fw.tail
    if unpack(tokenize(stream)[0]) != bytes(image):
        raise FirmwareError("change is not literal-only; pick another instruction encoding")
    return pack(fw.name, fw.address, fw.area, stream)


def image_diff(a: Firmware, b: Firmware) -> List[int]:
    """Image offsets where the two firmwares differ."""
    if len(a.image) != len(b.image):
        raise FirmwareError("images differ in length")
    return [i for i in range(len(a.image)) if a.image[i] != b.image[i]]
