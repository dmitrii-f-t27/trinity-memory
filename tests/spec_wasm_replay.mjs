// WASM consumer for the codec vectors of conformance/memory_types.json.
// The browser codec exposes a five-value buffer and an eight-byte output, so
// only groups of at most five trits with payloads of at most eight bytes are
// replayed here; the count of skipped vectors is reported, never hidden.
import {readFileSync} from 'node:fs';
const [wasmPath, vectorsPath] = process.argv.slice(2);
if (!wasmPath || !vectorsPath) { console.error('usage: spec_wasm_replay.mjs <codecs.wasm> <memory_types.json>'); process.exit(2); }
const codecs = {baseline2: 0, dense5: 1, dense17: 2, dense22: 3, sparse41: 4, sparse82: 5};
const binary = readFileSync(wasmPath);
const {instance} = await WebAssembly.instantiate(binary);
const api = instance.exports;
const values = new Int32Array(api.memory.buffer, api.tm_wasm_values(), 5);
const output = new Uint8Array(api.memory.buffer, api.tm_wasm_output(), 8);
const document = JSON.parse(readFileSync(vectorsPath, 'utf8'));
let checks = 0, skipped = 0;
const failures = [];
const hex = bytes => Array.from(bytes, b => b.toString(16).padStart(2, '0')).join('');
for (const vector of document.vectors) {
  if (vector.kind === 'group') {
    const trits = vector.trits;
    const payload = vector.payload_hex.length / 2;
    if (trits.length > 5 || payload > 8) { skipped++; continue; }
    values.fill(0); trits.forEach((t, i) => { values[i] = t; });
    output.fill(0);
    const written = api.tm_encode(codecs[vector.codec], values.byteOffset, trits.length, output.byteOffset, 8);
    if (written !== BigInt(payload) || hex(output.subarray(0, payload)) !== vector.payload_hex) { failures.push(`${vector.id}: encode ${hex(output.subarray(0, payload))}`); continue; }
    values.fill(0);
    const status = api.tm_decode(codecs[vector.codec], output.byteOffset, payload, trits.length, values.byteOffset, 5);
    if (status !== 0 || trits.some((t, i) => values[i] !== t)) { failures.push(`${vector.id}: decode status ${status}`); continue; }
    checks += 2;
  } else if (vector.kind === 'invalid_word') {
    const payload = vector.payload_hex.length / 2;
    if (vector.count > 5 || payload > 8) { skipped++; continue; }
    output.fill(0);
    for (let i = 0; i < payload; i++) output[i] = parseInt(vector.payload_hex.slice(2 * i, 2 * i + 2), 16);
    const status = api.tm_decode(codecs[vector.codec], output.byteOffset, payload, vector.count, values.byteOffset, 5);
    if (status >= 0) { failures.push(`${vector.id}: accepted`); continue; }
    checks++;
  } else if (vector.kind === 'invalid_sparsity') {
    const trits = vector.trits;
    if (trits.length > 5) { skipped++; continue; }
    values.fill(0); trits.forEach((t, i) => { values[i] = t; });
    const written = api.tm_encode(codecs[vector.codec], values.byteOffset, trits.length, output.byteOffset, 8);
    if (written >= 0n) { failures.push(`${vector.id}: encoded`); continue; }
    checks++;
  }
}
const report = {consumer: 'wasm', evidence: 'wasm', checks, skipped, failures};
console.log(JSON.stringify(report));
process.exit(failures.length ? 1 : 0);
