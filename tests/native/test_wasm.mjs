// Independent exhaustive browser-target evidence. Expected base-3 enumeration
// is a test oracle, never shipped as an alternative browser implementation.
import {readFileSync, writeFileSync} from 'node:fs';
import assert from 'node:assert/strict';
const binary = readFileSync(new URL('../../build/t27/codecs.wasm', import.meta.url));
const {instance} = await WebAssembly.instantiate(binary);
const api = instance.exports;
const values = new Int32Array(api.memory.buffer, api.tm_wasm_values(), 5);
const output = new Uint8Array(api.memory.buffer, api.tm_wasm_output(), 8);
let checks = 0;
for (let word = 0; word < 243; word++) {
  let digits = word;
  for (let i = 0; i < 5; i++) { values[i] = digits % 3 - 1; digits = Math.floor(digits / 3); }
  const expected = [...values];
  assert.equal(api.tm_encode(1, values.byteOffset, 5, output.byteOffset, 8), 1n);
  assert.equal(output[0], word);
  values.fill(0);
  assert.equal(api.tm_decode(1, output.byteOffset, 1, 5, values.byteOffset, 5), 0);
  assert.deepEqual([...values], expected);
  checks += 2;
}
for (let word = 243; word < 256; word++) {
  output[0] = word;
  assert.ok(api.tm_decode(1, output.byteOffset, 1, 5, values.byteOffset, 5) < 0);
  checks++;
}
for (const value of [-2147483648, -2, 2, 2147483647]) {
  values.fill(0); values[2] = value;
  assert.ok(api.tm_encode(1, values.byteOffset, 5, output.byteOffset, 8) < 0n);
  checks++;
}
values.fill(0);
assert.ok(api.tm_encode(1, values.byteOffset, 5, output.byteOffset, 0) < 0n); checks++;
const report = {evidence:'actual-wasm-generated-from-t27', checks, dense_words:243,
  reserved_words:13, invalid_trits:4, imports:WebAssembly.Module.imports(new WebAssembly.Module(binary))};
assert.equal(report.imports.length, 0);
writeFileSync(new URL('../../build/t27/wasm-validation.json', import.meta.url), JSON.stringify(report, null, 2) + '\n');
console.log(JSON.stringify(report));
