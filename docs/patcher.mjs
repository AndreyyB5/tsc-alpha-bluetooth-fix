// Patch logic of the web page. Pure functions: the same code runs in the
// browser and in `node --test tests/`. Mirrors patch.py.

export const DUAL = new TextEncoder().encode('BT MODE Dual\r\n');
export const LOGO_MAX = { width: 320, height: 140 };
// The home-screen logo area of the Alpha-40L: 320 px wide, top at y = 60.
export const LOGO_SIZE = { width: 320, height: 138 };
// Screen background colour of the Alpha-40L (RGB 32,24,16 = 0x20C2 in RGB565).
export const SCREEN_BG = [32, 24, 16];

export function toHex(bytes) {
  return Array.from(bytes, (b) => b.toString(16).padStart(2, '0')).join('');
}

export function fromHex(hex) {
  const out = new Uint8Array(hex.length / 2);
  for (let i = 0; i < out.length; i++) out[i] = parseInt(hex.substr(i * 2, 2), 16);
  return out;
}

export async function sha256Hex(bytes) {
  const digest = await globalThis.crypto.subtle.digest('SHA-256', bytes);
  return toHex(new Uint8Array(digest));
}

/** Rows of the 138-row logo that stay visible when the Alpha-30L shows its IP address line. */
export const LOGO_ROWS_30L = 107;

/** Speed code of the BM78 module: byte 1 of the EEPROM word the printer answers to OUT PEEK32(547429728). */
export function moduleCode(word) {
  const text = String(word).trim();
  if (text === '') return null;
  const n = Number(text);
  if (!Number.isInteger(n) || n < 0 || n > 0xffffffff) return null;
  return Math.floor(n / 256) % 256;
}

/** Every published patch for this official file: one per module speed. */
export function variantsFor(sha, patches) {
  return patches.filter((p) => p.input.sha256 === sha);
}

/** The variant for this module speed code (hex string in the patch file), or null. */
export function variantForCode(code, variants) {
  return variants.find((p) => p.module && p.module.speed_code && parseInt(p.module.speed_code, 16) === code) || null;
}

/** -> { patch, state: 'official' | 'fixed' } or null. */
export function findPatch(sha, patches) {
  for (const patch of patches) {
    if (patch.input.sha256 === sha) return { patch, state: 'official' };
    if (patch.output.sha256 === sha) return { patch, state: 'fixed' };
  }
  return null;
}

export function applyEdits(bytes, patch) {
  if (bytes.length !== patch.input.size) throw new Error('size does not match the official file');
  const out = new Uint8Array(bytes);
  for (const e of patch.edits) {
    const from = fromHex(e.from);
    const to = fromHex(e.to);
    for (let i = 0; i < from.length; i++) {
      if (out[e.offset + i] !== from[i]) throw new Error(`unexpected byte at offset ${e.offset + i}`);
    }
    out.set(to, e.offset);
  }
  return out;
}

/** -> { width, height } of a logo the printer can show; throws otherwise. */
export function checkBmp(bmp) {
  if (bmp.length < 54 || bmp[0] !== 0x42 || bmp[1] !== 0x4d) throw new Error('not a BMP file');
  const v = new DataView(bmp.buffer, bmp.byteOffset, bmp.byteLength);
  const offset = v.getUint32(10, true);
  const header = v.getUint32(14, true);
  const width = v.getInt32(18, true);
  const height = v.getInt32(22, true);
  const bpp = v.getUint16(28, true);
  const compression = v.getUint32(30, true);
  if (offset !== 54 || header !== 40 || bpp !== 24 || compression !== 0) {
    throw new Error('the logo must be a plain 24-bit BMP');
  }
  if (!(width > 0 && width <= LOGO_MAX.width && height > 0 && height <= LOGO_MAX.height)) {
    throw new Error(`the logo is ${width}×${height}; it must fit ${LOGO_MAX.width}×${LOGO_MAX.height}`);
  }
  if (bmp.length < 54 + (((width * 3 + 3) & ~3) * height)) throw new Error('the BMP file is truncated');
  return { width, height };
}

/** True if the logo draws something below the first `rows` rows (covered by the IP address line on the Alpha-30L). */
export function coveredRows(bmp, rows) {
  const { width, height } = checkBmp(bmp);
  const pad = (width * 3 + 3) & ~3;
  for (let y = rows; y < height; y++) {
    const at = 54 + (height - 1 - y) * pad;
    for (let x = 0; x < width; x++) {
      const [b, g, r] = [bmp[at + x * 3], bmp[at + x * 3 + 1], bmp[at + x * 3 + 2]];
      if (Math.max(Math.abs(r - SCREEN_BG[0]), Math.abs(g - SCREEN_BG[1]), Math.abs(b - SCREEN_BG[2])) > 12) return true;
    }
  }
  return false;
}

/** 24-bit bottom-up BMP from canvas RGBA pixels (alpha is ignored: draw on a solid background first). */
export function bmpFromRGBA(rgba, width, height) {
  const row = (width * 3 + 3) & ~3;
  const size = 54 + row * height;
  const out = new Uint8Array(size);
  const v = new DataView(out.buffer);
  out[0] = 0x42; out[1] = 0x4d;
  v.setUint32(2, size, true);
  v.setUint32(10, 54, true);
  v.setUint32(14, 40, true);
  v.setInt32(18, width, true);
  v.setInt32(22, height, true);
  v.setUint16(26, 1, true);
  v.setUint16(28, 24, true);
  v.setUint32(34, row * height, true);
  v.setInt32(38, 2835, true);
  v.setInt32(42, 2835, true);
  for (let y = 0; y < height; y++) {
    const dst = 54 + (height - 1 - y) * row;
    for (let x = 0; x < width; x++) {
      const src = (y * width + x) * 4;
      out[dst + x * 3] = rgba[src + 2];
      out[dst + x * 3 + 1] = rgba[src + 1];
      out[dst + x * 3 + 2] = rgba[src];
    }
  }
  return out;
}

export function concat(...parts) {
  const out = new Uint8Array(parts.reduce((n, p) => n + p.length, 0));
  let at = 0;
  for (const p of parts) { out.set(p, at); at += p.length; }
  return out;
}

const ascii = (s) => new TextEncoder().encode(s);

/** TSPL that stores the BMP as /system/LOGO.LCD (DOWNLOAD refuses that drive, COPY does not). */
export function logoCommands(bmp) {
  checkBmp(bmp);
  return concat(
    ascii(`\r\nDOWNLOAD F,"LOGO.LCD",${bmp.length},`), bmp,
    ascii('\r\nCOPY F,"LOGO.LCD",S,"LOGO.LCD"\r\nKILL F,"LOGO.LCD"\r\n'),
  );
}

/** Logo commands, then Bluetooth mode, then the firmware - the order that was tested. */
export function build({ firmware = null, dual = false, logo = null }) {
  const parts = [];
  if (logo) parts.push(logoCommands(logo));
  if (dual) parts.push(DUAL);
  if (firmware) parts.push(firmware);
  return concat(...parts);
}
