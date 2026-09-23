// Replays every vector of conformance/formats_*.json through build/t27/formats.wasm,
// the WASM build of t27/formats.t27 (tools/build-t27-wasm.sh). Decoding and
// encoding run in the module; this script only moves bytes and compares.
import {readFileSync, writeFileSync} from 'node:fs';
import assert from 'node:assert/strict';

const root = new URL('../', import.meta.url);
const binary = readFileSync(new URL('build/t27/formats.wasm', root));
const module = new WebAssembly.Module(binary);
assert.equal(WebAssembly.Module.imports(module).length, 0, 'formats.wasm must not import anything');
const {exports: api} = new WebAssembly.Instance(module, {});
const heapBase = Number(api.__heap_base.value);

let top = heapBase;
const reset = () => { top = (heapBase + 15) & ~15; };
function alloc(bytes) {
  const at = top;
  top = (top + Math.max(bytes, 1) + 15) & ~15;
  const missing = top - api.memory.buffer.byteLength;
  if (missing > 0) api.memory.grow(Math.ceil(missing / 65536));
  return at;
}
const u8 = (at, n) => new Uint8Array(api.memory.buffer, at, n);
const i32 = (at, n) => new Int32Array(api.memory.buffer, at, n);
const u32 = (at, n) => new Uint32Array(api.memory.buffer, at, n);
const i64 = (at, n) => new BigInt64Array(api.memory.buffer, at, n);
function bytesIn(hex) {
  const at = alloc(hex.length / 2);
  u8(at, hex.length / 2).set(Buffer.from(hex, 'hex'));
  return at;
}
function valuesIn(hex) {
  const raw = Buffer.from(hex, 'hex');
  const at = alloc(4 * raw.length);
  i32(at, raw.length).set(Int8Array.from(raw));
  return [at, raw.length];
}
function wordsIn(words) {
  const at = alloc(4 * words.length);
  u32(at, words.length).set(words);
  return at;
}
const valuesHex = (at, n) => Buffer.from(Int8Array.from(i32(at, n)).buffer).toString('hex');

const FLAGS = ['outside_ternary', 'noncanonical_base3', 'scale_negative', 'scale_zero',
  'trailer_nonzero', 'padding_nonzero', 'affine_not_ternary'];
const KIND = {F16: 1, BF16: 2, F32: 3};
const flagsOut = () => { const at = alloc(8 * FLAGS.length); i64(at, FLAGS.length).fill(0n); return at; };
const flagsOf = (at) => Object.fromEntries(FLAGS.map((token, slot) => [token, Number(i64(at, FLAGS.length)[slot])]));
const sentinel = (n) => { const at = alloc(n); u8(at, n).fill(0x77); return at; };
const untouched = (at, n) => u8(at, n).every((b) => b === 0x77);

let checks = 0;
const counts = {};

function block(v, ids) {
  const format = ids[v.format];
  const data = bytesIn(v.data_hex), size = v.data_hex.length / 2;
  const values = alloc(4 * v.count), scales = alloc(4 * 64), flags = flagsOut();
  const status = Number(api.tf_decode_blocks(format, data, size, v.count, values, v.count, scales, 64));
  assert.equal(status, v.expect.status, v.id);
  assert.equal(Number(api.tf_block_flags(format, data, size, v.count, flags)), Math.min(status, 0), v.id);
  if (status < 0) {
    // Silent output: the upstream loop reads neither the scale nor a padding digit.
    for (const entry of v.silent_output.filter((e) => e.values_hex !== undefined)) {
      const per = api.tf_block_elements(format), bytes = api.tf_block_bytes(format), at = api.tf_scale_offset(format);
      const trits = alloc(4 * v.count);
      for (let e = 0; e < v.count; e++) {
        i32(trits, v.count)[e] = api.tf_block_value(format, data, Math.floor(e / per) * bytes, e % per);
      }
      assert.equal(valuesHex(trits, v.count), entry.values_hex, v.id);
      const words = [];
      for (let b = 0; b < v.count / per; b++) { const w = u8(data + b * bytes + at, 2); words.push(w[0] | (w[1] << 8)); }
      assert.deepEqual(words, entry.scale_words, v.id);
    }
    return;
  }
  assert.equal(valuesHex(values, v.count), v.expect.values_hex, v.id);
  assert.deepEqual([...u32(scales, v.expect.scale_words.length)], v.expect.scale_words, v.id);
  assert.deepEqual(flagsOf(flags), v.expect.flags, v.id);
  if (v.encode) {
    const out = alloc(size);
    assert.equal(Number(api.tf_encode_blocks(format, values, v.count, scales, v.expect.scale_words.length, out, size)), size, v.id);
    assert.equal(Buffer.from(u8(out, size)).toString('hex'), v.data_hex, v.id);
  }
}

function blockEncode(v, ids) {
  const [values, count] = valuesIn(v.values_hex);
  const out = sentinel(4096);
  const status = Number(api.tf_encode_blocks(ids[v.format], values, count, wordsIn(v.scale_words), v.scale_words.length, out, 4096));
  assert.equal(status, v.expect.status, v.id);
  assert.ok(untouched(out, 4096), v.id);
}

function gguf(v) {
  const data = bytesIn(v.gguf_hex), size = v.gguf_hex.length / 2;
  assert.ok(v.file_size <= Number.MAX_SAFE_INTEGER, v.id);
  const name = Buffer.from(v.tensor);
  const nameAt = alloc(name.length); u8(nameAt, name.length).set(name);
  const info = alloc(api.tf_wasm_info_size());
  assert.equal(api.tf_gguf_find(data, size, nameAt, name.length, info), v.expect.find, v.id);
  const field = (index) => Number(api.tf_wasm_info_field(info, index));
  assert.equal(field(0), v.expect.ggml_type, v.id);
  assert.equal(field(7), v.expect.offset, v.id);
  assert.equal(field(8), v.expect.alignment, v.id);
  assert.equal(field(10), v.expect.prism ? 1 : 0, v.id);
  assert.equal(field(11), v.expect.bitnet ? 1 : 0, v.id);
  assert.equal(field(12), v.expect.next_offset, v.id);
  assert.equal(field(14), v.expect.has_next ? 1 : 0, v.id);
  assert.equal(field(15), v.expect.prev_end, v.id);
  assert.equal(field(16), v.expect.has_prev ? 1 : 0, v.id);
  assert.equal(field(6), v.expect.data_start, v.id);
  assert.equal(api.tf_gguf_check(info, BigInt(v.file_size)), v.expect.check, v.id);
}

function safetensors(v) {
  const data = bytesIn(v.safetensors_hex), size = v.safetensors_hex.length / 2;
  const name = Buffer.from(v.tensor);
  const nameAt = alloc(name.length); u8(nameAt, name.length).set(name);
  const tokenCount = 512, tokens = alloc(tokenCount * api.tf_wasm_json_token_size()), arena = alloc(8192);
  const info = alloc(api.tf_wasm_safe_info_size());
  assert.equal(api.tf_safetensors_find(data, size, nameAt, name.length, tokens, tokenCount, arena, 8192, info),
    v.expect.find, v.id);
  const field = (index) => Number(api.tf_wasm_safe_info_field(info, index));
  assert.equal(field(7), v.expect.begin, v.id);
  assert.equal(field(8), v.expect.end, v.id);
  assert.equal(field(11), v.expect.next_begin, v.id);
  assert.equal(field(12), v.expect.has_next ? 1 : 0, v.id);
  assert.equal(field(13), v.expect.prev_end, v.id);
  assert.equal(field(14), v.expect.has_prev ? 1 : 0, v.id);
  if (v.expect.find !== 0) {
    // A refused header: the check is the find status.
    assert.equal(v.expect.check, v.expect.find, v.id);
    return;
  }
  assert.equal(Buffer.from(u8(field(0), field(1))).toString(), v.expect.dtype, v.id);
  assert.deepEqual([field(3), field(4), field(5), field(6)].slice(0, field(2)), v.expect.shape, v.id);
  assert.equal(field(10), v.expect.dtype_bits, v.id);
  assert.equal(api.tf_safetensors_check(info, BigInt(v.file_size)), v.expect.check, v.id);
}

function i2s(v) {
  const data = bytesIn(v.data_hex), size = v.data_hex.length / 2;
  const values = alloc(4 * v.count), scale = alloc(4), flags = flagsOut();
  const status = Number(api.tf_decode_i2s(data, size, v.count, values, v.count, scale));
  assert.equal(status, v.expect.status, v.id);
  assert.equal(Number(api.tf_i2s_flags(data, size, v.count, flags)), Math.min(status, 0), v.id);
  if (status < 0) {
    for (const entry of v.silent_output.filter((e) => e.values_hex !== undefined)) {
      // The codes do not depend on the scale: read them with a scale of 1.0.
      assert.equal(status, -58, v.id);
      const patched = bytesIn(v.data_hex);
      u8(patched + v.count / 4, 4).set([0, 0, 0x80, 0x3f]);
      assert.ok(Number(api.tf_decode_i2s(patched, size, v.count, values, v.count, scale)) >= 0, v.id);
      assert.equal(valuesHex(values, v.count), entry.values_hex, v.id);
      assert.equal(Buffer.from(u8(data + v.count / 4, 4)).readUInt32LE(0), entry.scale_word, v.id);
    }
    return;
  }
  assert.equal(valuesHex(values, v.count), v.expect.values_hex, v.id);
  assert.equal(u32(scale, 1)[0], v.expect.scale_word, v.id);
  assert.deepEqual(flagsOf(flags), v.expect.flags, v.id);
  // A flagged vector's upstream view: the same trits with code 3 (+2 here) read as code3_value.
  for (const entry of (v.silent_output || []).filter((e) => e.values_hex !== undefined)) {
    const mapped = alloc(4 * v.count);
    for (let e = 0; e < v.count; e++) {
      const t = i32(values, v.count)[e];
      i32(mapped, v.count)[e] = t === 2 ? entry.code3_value : t;
    }
    assert.equal(valuesHex(mapped, v.count), entry.values_hex, v.id);
  }
  if (v.encode) {
    const out = alloc(size);
    assert.equal(Number(api.tf_encode_i2s(values, v.count, v.expect.scale_word, out, size)), size, v.id);
    assert.equal(Buffer.from(u8(out, size)).toString('hex'), v.data_hex, v.id);
  }
}

function i2sEncode(v) {
  const [values, count] = valuesIn(v.values_hex);
  const out = sentinel(4096);
  assert.equal(Number(api.tf_encode_i2s(values, count, v.scale_word, out, 4096)), v.expect.status, v.id);
  assert.ok(untouched(out, 4096), v.id);
}

function hf(v) {
  const data = bytesIn(v.data_hex), size = v.data_hex.length / 2, n = v.rows * v.cols;
  const values = alloc(4 * n), flags = flagsOut();
  const status = Number(api.tf_decode_hf_packed(data, size, v.rows, v.cols, values, n));
  assert.equal(status, v.expect.status, v.id);
  if (status < 0) return;
  assert.equal(Number(api.tf_scales_check(wordsIn([v.scale_word]), 1, KIND[v.scale_kind], flags)), v.expect.scale_status, v.id);
  i64(flags, FLAGS.length)[0] = BigInt(status);
  assert.equal(valuesHex(values, n), v.expect.values_hex, v.id);
  assert.deepEqual(flagsOf(flags), v.expect.flags, v.id);
  if (v.encode) {
    const out = alloc(size);
    assert.equal(Number(api.tf_encode_hf_packed(values, v.rows, v.cols, out, size)), size, v.id);
    assert.equal(Buffer.from(u8(out, size)).toString('hex'), v.data_hex, v.id);
  }
}

function hfEncode(v) {
  const [values] = valuesIn(v.values_hex);
  const out = sentinel(4096);
  assert.equal(Number(api.tf_encode_hf_packed(values, v.rows, v.cols, out, 4096)), v.expect.status, v.id);
  assert.ok(untouched(out, 4096), v.id);
}

function mlx(v) {
  const data = bytesIn(v.data_hex), size = v.data_hex.length / 2, n = v.rows * v.cols;
  const values = alloc(4 * n), flags = flagsOut();
  const status = Number(api.tf_decode_mlx2(data, size, v.rows, v.cols, v.group, values, n));
  assert.equal(status, v.expect.status, v.id);
  if (status < 0) return;
  const affine = Number(api.tf_affine_check(wordsIn(v.scale_words), wordsIn(v.bias_words), v.scale_words.length,
    KIND[v.scale_kind], flags));
  assert.equal(affine, v.expect.affine_status, v.id);
  i64(flags, FLAGS.length)[0] = BigInt(status);
  assert.equal(valuesHex(values, n), v.expect.values_hex, v.id);
  assert.deepEqual(flagsOf(flags), v.expect.flags, v.id);
  if (v.encode) {
    const out = alloc(size);
    assert.equal(Number(api.tf_encode_mlx2(values, v.rows, v.cols, v.group, out, size)), size, v.id);
    assert.equal(Buffer.from(u8(out, size)).toString('hex'), v.data_hex, v.id);
  }
}

function mlxEncode(v) {
  const [values] = valuesIn(v.values_hex);
  const out = sentinel(4096);
  assert.equal(Number(api.tf_encode_mlx2(values, v.rows, v.cols, v.group, out, 4096)), v.expect.status, v.id);
  assert.ok(untouched(out, 4096), v.id);
}

function onnx(v) {
  const data = bytesIn(v.data_hex), size = v.data_hex.length / 2, n = v.n * v.k;
  const zp = bytesIn(v.zero_points_hex), zpSize = v.zero_points_hex.length / 2;
  const values = alloc(4 * n), flags = flagsOut();
  const status = Number(api.tf_decode_onnx2(data, size, v.n, v.k, v.block_size, zp, zpSize, values, n));
  assert.equal(status, v.expect.status, v.id);
  if (status < 0) return;
  assert.equal(Number(api.tf_scales_check(wordsIn(v.scale_words), v.scale_words.length, KIND[v.scale_kind], flags)),
    v.expect.scale_status, v.id);
  i64(flags, FLAGS.length)[0] = BigInt(status);
  i64(flags, FLAGS.length)[5] = api.tf_onnx2_padding_nonzero(data, size, v.n, v.k, v.block_size);
  assert.equal(valuesHex(values, n), v.expect.values_hex, v.id);
  assert.deepEqual(flagsOf(flags), v.expect.flags, v.id);
  if (v.encode) {
    const out = alloc(size);
    assert.equal(Number(api.tf_encode_onnx2(values, v.n, v.k, v.block_size, zp, zpSize, out, size)), size, v.id);
    assert.equal(Buffer.from(u8(out, size)).toString('hex'), v.data_hex, v.id);
  }
}

function onnxEncode(v) {
  const [values] = valuesIn(v.values_hex);
  const zp = bytesIn(v.zero_points_hex);
  const out = sentinel(4096);
  assert.equal(Number(api.tf_encode_onnx2(values, v.n, v.k, v.block_size, zp, v.zero_points_hex.length / 2, out, 4096)),
    v.expect.status, v.id);
  assert.ok(untouched(out, 4096), v.id);
}

// A declared reject class needs kind "reject" and the status of that class;
// every reject, flag or silent vector carries its upstream readers' view, each
// cite naming lines of a file pinned for that upstream in the document.
const CITE = /^[A-Za-z0-9_./-]+:[0-9]+(-[0-9]+)?$/;
let rejects = 0;
function negative(v, document) {
  assert.equal(v.kind === 'reject', v.error_class !== undefined, v.id);
  const isNegative = ['error_class', 'flag_class', 'silent_class'].some((key) => v[key] !== undefined);
  assert.equal(isNegative, v.silent_output !== undefined, v.id);
  if (v.error_class !== undefined) {
    const status = document.constants.errors[v.error_class];
    assert.ok(status < 0, v.id);
    const e = v.expect;
    assert.ok([e.status, e.check, e.scale_status, e.affine_status].includes(status), v.id);
    rejects++;
  }
  for (const entry of v.silent_output || []) {
    assert.ok(['decodes', 'rejects', 'not_applicable', 'unknown'].includes(entry.behaviour), v.id);
    assert.equal(entry.behaviour === 'unknown', entry.cite.includes('UNKNOWN'), v.id);
    assert.ok(entry.cite.length > 0 && entry.note.length > 0, v.id);
    for (const cite of entry.cite.filter((c) => c !== 'UNKNOWN')) {
      assert.match(cite, CITE, v.id);
      assert.ok(document.upstream[entry.upstream].files.includes(cite.split(':')[0]), `${v.id}: ${cite}`);
    }
  }
}

const handlers = {block, block_encode: blockEncode, gguf, i2s, i2s_encode: i2sEncode, hf_packed: hf,
  hf_encode: hfEncode, mlx, mlx_encode: mlxEncode, onnx, onnx_encode: onnxEncode, safetensors};
for (const family of ['llama_cpp', 'prismml', 'bitnet_cpp', 'hf_bitnet', 'mlx', 'onnx']) {
  const document = JSON.parse(readFileSync(new URL(`conformance/formats_${family}.json`, root), 'utf8'));
  const ids = document.constants.format_ids;
  for (const vector of document.vectors) {
    reset();
    negative(vector, document);
    const kind = vector.kind === 'reject' ? vector.reader : vector.kind;
    const handler = handlers[kind];
    assert.ok(handler, `${family}: unknown kind ${kind}`);
    handler(vector, ids);
    counts[family] = (counts[family] || 0) + 1;
    checks++;
  }
}

// One call on a tensor far larger than the initial 4 MiB of memory: 4096 x 4096
// TQ2_0 weights (64 MiB of values). Memory may grow to 4 GiB. Every code byte
// is 0x24 (codes 0, 1, 2, 0 at bits 0, 2, 4, 6), so weight i of a block is
// {-1, 0, +1, -1}[(i % 128) / 32] (dequantize_row_tq2_0: j, l, m order); block
// b's scale word is 0x3c00 + (b & 0xff). The outputs start as sentinels, so a
// call that writes nothing, or stops early, fails.
reset();
const tq2 = JSON.parse(readFileSync(new URL('conformance/formats_llama_cpp.json', root), 'utf8')).constants.format_ids.TQ2_0;
const weights = 4096 * 4096, blocks = weights / 256;
const data = alloc(blocks * 66);
const tensor = u8(data, blocks * 66);
tensor.fill(0x24);
for (let b = 0; b < blocks; b++) { tensor[b * 66 + 64] = b & 0xff; tensor[b * 66 + 65] = 0x3c; }
const values = alloc(4 * weights), scales = alloc(4 * blocks);
i32(values, weights).fill(0x7f7f7f7f);
u32(scales, blocks).fill(0xdeadbeef);
assert.equal(Number(api.tf_decode_blocks(tq2, data, blocks * 66, weights, values, weights, scales, blocks)), 0);
const decoded = i32(values, weights), pattern = [-1, 0, 1, -1];
for (let i = 0; i < weights; i++) {
  if (decoded[i] !== pattern[(i % 128) >> 5]) assert.fail(`large tensor: weight ${i} is ${decoded[i]}`);
}
const words = u32(scales, blocks);
for (let b = 0; b < blocks; b++) {
  if (words[b] !== 0x3c00 + (b & 0xff)) assert.fail(`large tensor: scale ${b} is ${words[b]}`);
}

const report = {evidence: 'actual-wasm-generated-from-t27', vectors: checks, reject_vectors: rejects, per_family: counts,
  large_tensor_weights: weights, memory_bytes: api.memory.buffer.byteLength,
  imports: WebAssembly.Module.imports(module).length};
writeFileSync(new URL('build/t27/formats-wasm-replay.json', root), JSON.stringify(report, null, 2) + '\n');
console.log(`PASS formats.wasm replay: ${checks} vectors in 6 files (${rejects} rejections with their classes), ` +
  `${weights} weights in one call, 0 imports`);
