// Logic of the web page (docs/patcher.mjs). Run: node --test tests/
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import {
  applyEdits, bmpFromRGBA, build, checkBmp, findPatch, fromHex, logoCommands, sha256Hex, toHex,
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
