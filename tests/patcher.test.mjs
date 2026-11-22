// Logic of the web page (docs/patcher.mjs). Run: node --test tests/
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import {
  applyEdits, bmpFromRGBA, build, checkBmp, coveredRows, findPatch, fromHex, LOGO_ROWS_30L, logoCommands,
  moduleCode, sha256Hex, toHex, variantForCode, variantsFor,
} from '../docs/patcher.mjs';

const root = new URL('../', import.meta.url);
const json = (p) => JSON.parse(readFileSync(new URL(p, root), 'utf8'));

test('published patch files are consistent', () => {
  for (const name of json('docs/patches/index.json').patches) {
    const p = json(`docs/patches/${name}`);
    assert.match(p.input.sha256, /^[0-9a-f]{64}$/);
    assert.match(p.output.sha256, /^[0-9a-f]{64}$/);
    for (const e of p.edits) assert.equal(e.from.length, e.to.length);
  }
});

test('edits are applied byte by byte and checked', () => {
  const patch = { input: { size: 6 }, edits: [{ offset: 1, from: '0203', to: 'aabb' }] };
  assert.equal(toHex(applyEdits(fromHex('010203040506'), patch)), '01aabb040506');
  assert.throws(() => applyEdits(fromHex('01ff03040506'), patch));
  assert.throws(() => applyEdits(fromHex('0102'), patch));
});

test('findPatch tells official from already fixed', () => {
  const p = { input: { sha256: 'a' }, output: { sha256: 'b' } };
  assert.equal(findPatch('a', [p]).state, 'official');
  assert.equal(findPatch('b', [p]).state, 'fixed');
  assert.equal(findPatch('c', [p]), null);
});

test('one official file, one variant per module speed', () => {
  const all = json('docs/patches/index.json').patches.map((n) => json(`docs/patches/${n}`));
  const l40 = all.filter((p) => p.id.startsWith('alpha-40l'));
  const v = variantsFor(l40[0].input.sha256, all);
  assert.deepEqual(v.map((p) => p.variant), ['115200', '921600']);
  assert.equal(variantForCode(0x03, v).variant, '115200');
  assert.equal(variantForCode(0x11, v).variant, '921600');
  assert.equal(variantForCode(0x0b, v), null);
});

test('the module speed code is byte 1 of the printer\'s answer', () => {
  assert.equal(moduleCode('3205173505'), 0x11); // a real printer whose module runs at 921600
  assert.equal(moduleCode(0xbf0b0301), 0x03);
  assert.equal(moduleCode(' 3204646913 '), moduleCode(3204646913));
  for (const bad of ['', 'abc', '1.5', '-1', '99999999999']) assert.equal(moduleCode(bad), null);
});

test('the Alpha-30L has no firmware bytes to change and its own logo area', () => {
  const all = json('docs/patches/index.json').patches.map((n) => json(`docs/patches/${n}`));
  const l30 = all.find((p) => p.model.endsWith('Alpha-30L'));
  assert.deepEqual(l30.edits, []);
  assert.equal(l30.input.sha256, l30.output.sha256);
  assert.equal(l30.logo_rows, LOGO_ROWS_30L);
  const logo30 = new Uint8Array(readFileSync(new URL('docs/assets/marker-logo-30l.bmp', root)));
  const logo40 = new Uint8Array(readFileSync(new URL('docs/assets/marker-logo.bmp', root)));
  assert.equal(coveredRows(logo30, LOGO_ROWS_30L), false);
  assert.equal(coveredRows(logo40, LOGO_ROWS_30L), true);
});

test('sha256 matches the known value', async () => {
  assert.equal(await sha256Hex(new TextEncoder().encode('abc')),
    'ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad');
});

test('canvas pixels become a bottom-up 24-bit BMP the printer accepts', () => {
  const w = 3; const h = 2;
  const rgba = new Uint8Array(w * h * 4);
  rgba.set([255, 0, 0, 255], 0); // top-left red
  const bmp = bmpFromRGBA(rgba, w, h);
  assert.deepEqual(checkBmp(bmp), { width: 3, height: 2 });
  const row = (w * 3 + 3) & ~3;
  assert.deepEqual([...bmp.slice(54 + row, 54 + row + 3)], [0, 0, 255]); // BGR, last row in file = top
});

test('the Marker logo shipped with the page is valid', () => {
  assert.deepEqual(checkBmp(new Uint8Array(readFileSync(new URL('docs/assets/marker-logo.bmp', root)))),
    { width: 320, height: 138 });
});

test('file order: logo, then Dual mode, then firmware (same bytes as patch.py)', () => {
  const logo = bmpFromRGBA(new Uint8Array(4 * 4 * 4), 4, 4);
  const out = build({ firmware: new TextEncoder().encode('FW'), dual: true, logo });
  const text = new TextDecoder('latin1').decode(out);
  assert.ok(text.startsWith(`\r\nDOWNLOAD F,"LOGO.LCD",${logo.length},`));
  assert.ok(text.endsWith('KILL F,"LOGO.LCD"\r\nBT MODE Dual\r\nFW'));
  assert.equal(out.length, logoCommands(logo).length + 'BT MODE Dual\r\nFW'.length);
});
