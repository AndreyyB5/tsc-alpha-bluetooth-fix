# How the Alpha‑40L Bluetooth bug was found and fixed

Everything below was measured on two TSC Alpha‑40L with Microchip BM78 modules (TSC part RF‑BHS) and
one Alpha‑30L with a Lantronix xPico270, firmware M2.01.002 EZC, over USB (the 30L also over its Wi‑Fi
port 9100) with the printers' own service commands. Addresses are valid for this firmware only, and
differ between the two models.

## 1. Symptom

- Pairing works, «Test connection» passes, the Bluetooth icon is on.
- Nothing prints. With Bluetooth dump enabled the printer receives only `0x00` bytes.
- Sending 69 bytes produced **exactly 212 zero bytes** — the number of falling edges in those 69
  bytes at 115 200 baud. The CPU samples the line far too fast and turns every low pulse into a byte.
- A second printer of the same model, with the same firmware and module, was **not** cured by the first
  fix: after it the module's data arrived as random bytes, not zeros (§3).

## 2. Hardware in one paragraph

The CPU is a Microchip SAM9X60 (ARM926). The firmware runs from RAM at `0x20000000`. The BM78 sits on
FLEXCOM3 USART `0xF8028200` (MR `+0x04`, BRGR `+0x20`, RTOR `+0x24`), hardware handshake on, receive by
DMA (XDMAC channel 14) with a receiver time‑out. Master clock: 196 607 999 Hz. PC17 selects the
module's setup (EEPROM) mode, PC30 resets it. The module keeps its UART speed in its own EEPROM, byte `0x31`: `03` = 115 200,
`11` = 921 600. **It differs between printers of the same model** — read it, do not assume it (§3).

## 3. Root cause

| Function | Address | Speed it sets |
|---|---|---|
| `bt_enter_eeprom_mode` — firmware configures the module | `0x202EDC64` | 115 200 |
| `bt_enter_app_mode` — firmware opens the module for printing | `0x202EDBA4` | **1 000 000** |

With this clock BRGR becomes 12, which is 1 024 000 baud. The BM78 runs at the speed in its own EEPROM
and at nothing else (its «921 600» preset is really 941 176 = 16 MHz / 17). An earlier version of this
page said the firmware writes `0x31 = 03` during setup — **wrong**: neither M2.01.002 nor B1.15 writes
that byte, so whatever the module came with stays:

| | Printer A | Printer B |
|---|---|---|
| EEPROM byte `0x31` | `03` = 115 200 | `11` = 921 600 |
| M2.01.002 as shipped (1 024 000) | zeros only | nothing printed (not dumped) |
| 115 200 firmware variant | works | random bytes, nothing prints |
| 921 600 firmware variant (BRGR 13 → 945 230 baud, 0.4 % off) | — | works |

Reading the byte on a running printer: the firmware keeps a RAM copy of the EEPROM, so
`OUT PEEK32(547429728)` (address `0x20A11D60` = copy + `0x30`) answers one word; byte 1 of it is `0x31`.
`3205173505` = `0xBF0B1101` → `0x11`.

Proof:

1. `OUT PEEK32(4160913952)` (BRGR) read 12.
2. `POKE32 4160913952,106` (115 924 baud, 0.6 % off) — bytes arrived intact; a 12 603‑byte job came
   through byte‑identical and printed.
3. On printer B the same POKE left garbage; `POKE32 4160913952,13` (945 230 baud) with the matching
   time‑out made a 15 430‑byte job arrive intact and print.
4. On every reconnect the firmware set 1 000 000 again (through a function pointer at `0x228A3300`),
   so the fix has to be in the firmware itself or re‑applied at start (`AUTO.BAS`).

## 4. The patch

```
0x202EDBF0   f4 0a a0 e3   mov r0, #0xF4000          (original)
0x202EDBF4   90 0f 80 e3   orr r0, r0, #0x240        → 1 000 000

0x202EDBF0   07 09 a0 e3   mov r0, #0x1C000          (module code 03)
0x202EDBF4   80 0f 80 e3   orr r0, r0, #0x200        → 115 200

0x202EDBF0   e1 0a a0 e3   mov r0, #0xE1000          (module code 11)
0x202EDBF4   00 0f 80 e3   orr r0, r0, #0            → 921 600
```

Both variants change the same instruction pair of the same official file; `docs/patches/` holds one
definition per module speed. The check word is the first instruction: `OUT PEEK32(539941872)` →
`3818916103` (115 200), `3818916577` (921 600), `3818916596` (original).

Why this encoding and not the one the firmware uses elsewhere (`mov r0,#0x10000; orr r0,#0xC200`):
in TSC's compressed stream these 8 bytes are literals, but a later match copies the byte at
`0x202EDBF5`. This encoding keeps that byte (`0x0F`) and changes only three literal bytes, so no
compression token moves: same file length, same structure, 7 bytes different in total (3 code bytes,
the checksum in the header text and the inner checksum). The 921 600 encoding keeps the same byte and
needs only 2 code bytes.

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
RAM only: a restart without the file brings the original behaviour back. It is meant for modules at
115 200 (code `03`); `tools/tsc_usb.py autobas` refuses to install it on others. A 921 600 module needs
BRGR 13 and the time‑out 28 356 (30 ms at 945 230 baud) — the same POKEs with those numbers worked by
hand, but that script has not been run from power‑on.

## 7. Bluetooth modes and connections

- Settings byte `+0xACF` of the settings structure (pointer at `0x228A3FCC`):
  0 Dual, 1 BT2.1, 2 BT3.0, 3 BT4.0. The menu offers only single modes; `BT MODE Dual` (no quotes —
  `BT MODE "Dual"` is ignored) sets the third one.
- The firmware copies the mode into module EEPROM `0x1AF` **at start‑up only**: bit `0x40` = classic
  only, bit `0x20` = LE only, neither = Dual. Restart after changing it.
- **Flashing reset the mode to BT4.0 (LE only)** on the Alpha‑40L we measured: it was Dual before the
  flash and BT4.0 after the restart. Set it again after every flash.
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
- on the Alpha‑30L the IP address line fills canvas rows 167–199 and hides BMP rows 107–137 (§12);
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

`ALPHA30L_EZC_M2.01.002.NEW` (3 080 228 bytes, SHA‑256 `98636b38…ea728fa4`) is tagged `FuseA40L` like the
40L file. `tscfw.py` unpacks it and finds the same `bt_enter_app_mode` with the 1 000 000 constant at
`0x201D097C` (73 of 80 words identical) — for a **BM78** Alpha‑30L the 40L patch would apply, but we had
none to test. The Alpha‑30L we had has an **xPico270** (Lantronix Wi‑Fi + Bluetooth) and no speed bug.

### How the printer talks to the xPico270

- FLEXCOM3 (USART `0xF8028200`), 115 200 baud, hardware flow control. The xPico's line runs Lantronix's
  **Mux** protocol: the printer sends short commands (`1v1` → Trouble Log on channel 1, `2v2` → command line
  on channel 2, `3lSPP_1` / `4lGATTS_TSC` → bind channels, `3x\d0010@+@` → transparent mode on a channel)
  and reads `K` / `E …` answers and polls (`1r`, `3r`, `4r` = data on that channel).
- The module's settings are pushed as XML records (`<configgroup name="Bluetooth">…`,
  `GATT Server Custom Service "TSC"` with `…8841` writable and `…1E4D` readable).
- The printer reads the module's Trouble Log line by line and keeps one byte per event in the state block
  at `0x208958E0` (`+0x70`…`+0x77`: re‑bind request, GATT ready, SPP started, GATT registered, GATT
  bound, SPP bound, –, linked). Only «Started SPP» makes it bind `SPP_1`.
- **The ring buffer of everything the module sent** is at `0x2236A654` (start `+0x2C`, end `+0x30`, read
  `+0x38`, count `+0x3C`, 16 KB). Reading it back over USB shows every answer and log line — the most useful
  tool for this module. `DUMP_BT` does not work for the xPico path (it reports only `01` bytes).

### What we saw (module firmware 5.2.1.0R5)

- Start‑up: the module reports `HCI is up`, `Registered GATT server`, `Trouble Log started on Line:Virtual_1`,
  `Command Line started on Line:Virtual_2` — but never `Started SPP`, so classic Bluetooth stays unbound.
- Forcing the bind (`POKE32` on the state block) sends `3lSPP_1`; the module answers
  `E Invalid line name`. Classic Bluetooth (SPP) therefore does not work on this module with this
  printer firmware.
- A phone that connected over LE produced `GATT client connected`. Channel 4 was already bound to
  `GATTS_TSC` (`E Connection instance already in use` on a second bind), but **no `4r` (data) ever came**
  during that connection. The app wrote somewhere else, or not at all; the printer side waits correctly.
  Not verified: that a write to `…8841` prints.
- The printer was in BT4.0 (LE only) before `BT MODE Dual`.

### The home screen and the logo

The Wi‑Fi address line fills rows 167–199 of the 320 × 200 home canvas with the background colour and
covers BMP rows 107–137 of a 138‑row logo. Read from RAM: canvas pointer at `0x20884228 + 0x918`
(first word `width | height << 16`, then RGB565 pixels; the 40L uses `0x20A035A0 + 0x918`), the logo height
is the word at `+0x944`. The logo is drawn at x = 160, top y = 60, as on the 40L.
`docs/assets/marker-logo-30l.bmp` keeps its picture in rows 6–104.

