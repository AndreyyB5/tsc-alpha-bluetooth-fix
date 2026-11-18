# How the Alpha‑40L Bluetooth bug was found and fixed

Everything below was measured on a TSC Alpha‑40L with a Microchip BM78 module (TSC part RF‑BHS),
firmware M2.01.002 EZC, over USB with the printer's own service commands. Addresses are valid for
this firmware only.

## 1. Symptom

- Pairing works, «Test connection» passes, the Bluetooth icon is on.
- Nothing prints. With Bluetooth dump enabled the printer receives only `0x00` bytes.
- Sending 69 bytes produced **exactly 212 zero bytes** — the number of falling edges in those 69
  bytes at 115 200 baud. The CPU samples the line far too fast and turns every low pulse into a byte.

## 2. Hardware in one paragraph

The CPU is a Microchip SAM9X60 (ARM926). The firmware runs from RAM at `0x20000000`. The BM78 sits on
FLEXCOM3 USART `0xF8028200` (MR `+0x04`, BRGR `+0x20`, RTOR `+0x24`), hardware handshake on, receive by
DMA (XDMAC channel 14) with a receiver time‑out. Master clock: 196 607 999 Hz. PC17 selects the
module's setup (EEPROM) mode, PC30 resets it. The module's UART speed is its EEPROM byte `0x31`
(`03` = 115 200).

## 3. Root cause

| Function | Address | Speed it sets |
|---|---|---|
| `bt_enter_eeprom_mode` — firmware configures the module | `0x202EDC64` | 115 200 |
| `bt_enter_app_mode` — firmware opens the module for printing | `0x202EDBA4` | **1 000 000** |

The firmware also writes `0x31 = 03` (115 200) into the module during setup — then opens it for
printing at 1 000 000. With this clock BRGR becomes 12, which is 1 024 000 baud. The BM78 cannot run
at that speed at all (its nearest preset, «921 600», is really 941 176 = 16 MHz / 17).

Proof:

1. `OUT PEEK32(4160913952)` (BRGR) read 12.
2. `POKE32 4160913952,106` (115 924 baud, 0.6 % off) — bytes arrived intact; a 12 603‑byte job came
   through byte‑identical and printed.
3. On every reconnect the firmware set 1 000 000 again (through a function pointer at `0x228A3300`),
   so the fix has to be in the firmware itself or re‑applied at start (`AUTO.BAS`).

## 4. The patch

```
0x202EDBF0   f4 0a a0 e3   mov r0, #0xF4000          (original)
0x202EDBF4   90 0f 80 e3   orr r0, r0, #0x240        → 1 000 000

0x202EDBF0   07 09 a0 e3   mov r0, #0x1C000          (fixed)
0x202EDBF4   80 0f 80 e3   orr r0, r0, #0x200        → 115 200
```

Why this encoding and not the one the firmware uses elsewhere (`mov r0,#0x10000; orr r0,#0xC200`):
in TSC's compressed stream these 8 bytes are literals, but a later match copies the byte at
`0x202EDBF5`. This encoding keeps that byte (`0x0F`) and changes only three literal bytes, so no
compression token moves: same file length, same structure, 7 bytes different in total (3 code bytes,
the checksum in the header text and the inner checksum).

## 5. Firmware file format

```
"FuseA40L 00100000,00370000,19E9F842,"   flash address, payload length, payload byte-sum (hex)
payload, 0x370000 bytes:
  u32  byte-sum of everything after these 8 bytes        0x19E9F45E
  u32  n                                                   3 579 070
  n    "TS01" + LZSS stream
  ...  zero padding                                        25 402 bytes
```

- LZSS: Okumura variant — 4 096‑byte window filled with `0x20`, first write at 4 078, one flag byte
  per 8 tokens (bit 0 first, 1 = literal), match = 2 bytes (12‑bit position, 4‑bit length − 3),
  lengths 3–18. The stream ends with one stray byte where a match would start; decoders stop there.
- Unpacked image: 9 211 572 bytes, loaded at `0x20000000`.
- The update handler (`FuseA40L` command → `0x20410530`) parses the header, receives the whole payload
  into RAM, checks the byte‑sum, and only then erases and writes NAND at `0x100000` and restarts.
  There is no signature, and PRTSecure does not apply. A broken transfer changes nothing.
- NAND regions it can write: `0x0–0x40000`, `0xC0000–0x100000`, `0x100000–0x800000`. The block at
  `0x40000–0xC0000` (likely the bootloader) is not reachable through this path.
- **Alpha‑30L files are also tagged `FuseA40L`.** The printer may accept the other model's file —
  do not mix them up.

[`tscfw.py`](../tscfw.py) rebuilds TSC's file byte‑for‑byte from its parts, which is how the patch
was produced: `parse()` → change the image → `replace_in_place()` → `pack()`.

## 6. The no‑reflash fix (`tools/AUTO.BAS`)

The printer runs `/user/AUTO.BAS` at power‑on (file name in the settings, default `AUTO.BAS`). The
script checks model, firmware and that `0x2041E4EC` still holds `bx lr`, then:

| `POKE32` | Value | Meaning |
|---|---|---|
| `0x228A3300` | `0x2041E4EC` | point the baud setter at a `bx lr`: the firmware can no longer set 1 000 000 |
| `0x20A31B64` | 115 200 | the driver's own copy of the speed |
| `0xF8028220` | 106 | BRGR → 115 924 baud |
| `0xF8028224` | 3 456 | receiver time‑out for this speed (30 ms) |

The set is applied twice, 3 s apart, because the firmware re‑initialises the module while starting.
RAM only: a restart without the file brings the original behaviour back.

## 7. Bluetooth modes and connections

- Settings byte `+0xACF` of the settings structure (pointer at `0x228A3FCC`):
  0 Dual, 1 BT2.1, 2 BT3.0, 3 BT4.0. The menu offers only single modes; `BT MODE Dual` (no quotes —
  `BT MODE "Dual"` is ignored) sets the third one.
- The firmware copies the mode into module EEPROM `0x1AF` **at start‑up only**: bit `0x40` = classic
  only, bit `0x20` = LE only, neither = Dual. Restart after changing it.
- The module keeps one connection. A phone paired in its system settings holds it all the time and
  locks everyone else out («printer does not answer»).

## 8. Bluetooth LE

Service `49535343‑FE7D‑4AE5‑8FA9‑9FAFD205E455` (Microchip transparent UART):

| Characteristic | Properties |
|---|---|
| `…‑1E4D‑4BD9‑BA61‑23C647249616` (TX) | read, write, write without response, notify |
| `…‑8841‑43F4‑A8D4‑ECBE34729BB3` (RX) | write, write without response |
| `…‑6DAA‑…`, `…‑ACA3‑…`, `…‑026E‑…`, `…‑4C8A‑…` | control / configuration |

**Subscribe to TX notifications before sending data.** Without it the module passes only the first
packet (157 bytes at MTU 160) and stops acknowledging writes; pauses between packets do not help.
With the subscription 7 000 of 7 000 bytes arrive, also after the printer restarts — subscribe again
on every new connection.

How to measure delivery honestly (one short command passes even when streaming is broken):

```
USB:        DUMP_BT_CLEAR      A=0
Bluetooth:  1000 × "A=A+1\r\n"   (7 000 bytes)
USB:        OUT A              → 1000 if every byte arrived
```

## 9. Home‑screen logo

The home screen (`0x203D2F30`) draws `/system/LOGO.LCD` if it exists and then skips the status word
(«Ready», «Pause»):

- 24‑bit uncompressed BMP, 54‑byte header, bottom‑up rows, up to 320 × 140;
- centred at x = 160, top at y = 60 of the 320 × 200 home canvas (RGB565);
- background colour `0x20C2` = RGB 32, 24, 16;
- read at start‑up only.

`DOWNLOAD` refuses the system drive `S:`, but `COPY` does not check it:

```
DOWNLOAD F,"LOGO.LCD",<size>,<bmp bytes>
COPY F,"LOGO.LCD",S,"LOGO.LCD"
KILL F,"LOGO.LCD"
```

Remove: `KILL S,"LOGO.LCD"` and restart. Never send `KILL S,"*"`.

## 10. Service commands that helped

| Command | What it does |
|---|---|
| `OUT PEEK32(addr)` / `POKE32 addr,value` | read / write a 32‑bit word (decimal numbers) |
| `OUT GETSETTING$("CONFIG","BT","MODULE")` | Bluetooth module: `BM78 MFi` |
| `OUT GETSETTING$("SYSTEM","INFORMATION","MODEL")` | `Alpha-40L` |
| `BT MODE Dual` | Bluetooth mode (applied at start) |
| `BT DUMPEEPROM 1280` | read the module EEPROM (prints a hex dump) |
| `DUMP_BT start,count`, `DUMP_BT_CLEAR` | inspect / clear what the CPU received from the module |
| `ESC !R`, `ESC !?` | restart, status byte |
| `OUT LOF("file")` | file size (0 = absent) |

`BT WRTEEPROM` writes the module EEPROM — not needed for this fix. `FILES` prints its list on a label.

## 11. Memory map (Alpha‑40L, M2.01.002)

| Address | What |
|---|---|
| `0x202EDBA4` | `bt_enter_app_mode` — the bug |
| `0x202EDC64` | `bt_enter_eeprom_mode` |
| `0x2041E270` | `bt_uart_set_baud` |
| `0x2041E4EC` | a `bx lr` (used by `AUTO.BAS`) |
| `0x228A3300` | pointer to the baud setter |
| `0x20A31B64` | Bluetooth speed variable |
| `0x228A3FCC` | pointer to the settings structure |
| `0x20A11D30` | RAM copy of the module EEPROM |
| `0x2281CA54` | Bluetooth receive counters (count at `+12`) |
| `0x20A035A0` | UI state (`+0x918` home canvas, `+0x944` logo height) |
| `0x203D2F30` | home screen |
| `0x20410530` | firmware update handler |

## 12. Alpha‑30L

`ALPHA30L_EZC_M2.01.002.NEW` (SHA‑256 `98636b38…728fa4`) contains the same function: 73 of 80 words
are identical, only call offsets differ. The 1 000 000 constant is at `0x201D097C`, and the same
encoding again changes only three literal bytes. It will be published here after it is tested on a
real Alpha‑30L.
