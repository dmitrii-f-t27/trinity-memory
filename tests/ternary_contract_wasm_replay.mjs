// Replays the payload vectors of conformance/formats_*.json through the
// Ternary Check contract functions in build/t27/formats.wasm (the WASM build
// of t27/ternary_contract.t27): the strict reader and writer (tk_decode,
// tk_encode), the class tokens, the comparison (tk_compare_bytes,
// tk_compare_words, tk_flag_state) and the verdict (tk_verdict). Every call
// must end as `match` or `rejected`, as it does natively through
// `trinity-memory ternary-check run` (tests/test_ternary_check_run.py). This
// script only moves bytes; container vectors are outside contract v1.
import {readFileSync, writeFileSync} from 'node:fs';
import assert from 'node:assert/strict';

const root = new URL('../', import.meta.url);
const module = new WebAssembly.Module(readFileSync(new URL('build/t27/formats.wasm', root)));
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
const u32 = (at, n) => new Uint32Array(api.memory.buffer, at, n);
const i64 = (at, n) => new BigInt64Array(api.memory.buffer, at, n);
function bytesIn(data) {
  const at = alloc(data.length);
  u8(at, data.length).set(data);
  return at;
}
const hexIn = (hex) => bytesIn(Buffer.from(hex ?? '', 'hex'));
function wordsIn(words) {
  const at = alloc(4 * words.length);
  u32(at, words.length).set(words);
  return at;
}
function text(fn, value) {
  const at = alloc(64);
  const n = Number(fn(value, at, 64));
  return Buffer.from(u8(at, Math.max(n, 0))).toString('ascii');
}
function nameIn(name) {
  const data = Buffer.from(name, 'ascii');
  return [bytesIn(data), data.length];
}

const FLAG_COUNT = 7;
const KIND = {F16: 1, BF16: 2, F32: 3};
const READERS = {block: null, i2s: 'I2_S', hf_packed: 'HF_PACKED', mlx: 'MLX2', onnx: 'ONNX2'};
const WRITERS = {block_encode: null, i2s_encode: 'I2_S', hf_encode: 'HF_PACKED', mlx_encode: 'MLX2', onnx_encode: 'ONNX2'};
const SIDE = new Set(['HF_PACKED', 'MLX2', 'ONNX2']);

function formatId(name) {
  const [at, n] = nameIn(name);
  const id = api.tk_format_of_name(at, n);
  assert.ok(id > 0, name);
  assert.equal(text(api.tk_format_token, id), name);
  return id;
}

function geometry(v, name) {
  if (name === 'HF_PACKED' || name === 'MLX2') return [v.rows * v.cols, v.rows, v.cols, v.group ?? 0];
  if (name === 'ONNX2') return [v.n * v.k, v.n, v.k, v.block_size];
  return [v.count, 0, 0, 0];
}

function sideScales(v, name) {
  if (name === 'HF_PACKED') return [[v.scale_word], KIND[v.scale_kind]];
  if (name === 'MLX2' || name === 'ONNX2') return [v.scale_words, KIND[v.scale_kind]];
  return [[], 0];
}

const outcomes = {};
let calls = 0;

function judge(v, op, expected, status, compare) {
  // status: the reader's or writer's return; a rejection becomes the error
  // file of a decoder that exits 1 with the class token of that status.
  let exit = 0, error = 0, valueDiff = 0n, scaleDiff = 0n, flagState = 0;
  if (status < 0) {
    const token = text(api.tk_error_token, status);
    assert.ok(token.length > 0, `${v.id}: status ${status} has no class token`);
    const [at, n] = nameIn(token + '\n');
    exit = 1;
    error = api.tk_error_status(at, n);
    assert.equal(error, status, v.id);
  } else if (expected === 0) {
    [valueDiff, scaleDiff, flagState] = compare();
  }
  const outcome = api.tk_verdict(expected, 0, exit, error, valueDiff, scaleDiff, flagState);
  const name = text(api.tk_outcome_token, outcome);
  outcomes[name] = (outcomes[name] ?? 0) + 1;
  calls++;
  assert.ok(name === 'match' || name === 'rejected', `${v.id} ${op}: ${name}`);
  assert.equal(api.tk_fails(outcome, 0), 0, v.id);
}

function decode(v, name, errors, slots) {
  const format = formatId(name);
  const [count, rows, cols, group] = geometry(v, name);
  const [side, kind] = sideScales(v, name);
  const data = Buffer.from(v.data_hex, 'hex');
  const zp = Buffer.from(v.zero_points_hex ?? '', 'hex');
  const biases = v.bias_words ?? [];
  const capacity = Math.max(1, Number(api.tk_scale_count(format, count, rows, cols, group)), side.length);
  const work = alloc(4 * count), values = alloc(count), scales = alloc(4 * capacity), flags = alloc(8 * FLAG_COUNT);
  const status = Number(api.tk_decode(format, bytesIn(data), data.length, count, rows, cols, group, kind,
    wordsIn(side), side.length, wordsIn(biases), biases.length, bytesIn(zp), zp.length, work, values, count,
    scales, capacity, flags));
  const expected = v.error_class ? errors[v.error_class] : 0;
  judge(v, 'decode', expected, status, () => {
    const first = alloc(8);
    const e = v.expect;
    const valueDiff = api.tk_compare_bytes(values, count, hexIn(e.values_hex), e.values_hex.length / 2, first);
    const [words, width] = SIDE.has(name) ? [side, kind === KIND.F32 ? 4 : 2]
      : name === 'I2_S' ? [[e.scale_word], 4] : [e.scale_words, 2];
    assert.equal(Number(api.tk_scale_width(format, kind)), width, v.id);
    const stored = alloc(width * status);
    for (let i = 0; i < status; i++) {
      const w = u32(scales, status)[i];
      for (let b = 0; b < width; b++) u8(stored, width * status)[width * i + b] = (w >>> (8 * b)) & 255;
    }
    const scaleDiff = api.tk_compare_words(stored, width * status, width, wordsIn(words), words.length, first);
    // The reader's flags go through the contract's text form and back.
    const flagText = alloc(1024);
    const n = Number(api.tk_flags_text(flags, flagText, 1024));
    assert.ok(n >= 0, v.id);
    const parsed = alloc(8 * FLAG_COUNT);
    const parseStatus = api.tk_parse_flags(flagText, n, parsed);
    const expectedFlags = alloc(8 * FLAG_COUNT);
    i64(expectedFlags, FLAG_COUNT).fill(0n);
    for (const [token, value] of Object.entries(e.flags)) i64(expectedFlags, FLAG_COUNT)[slots[token]] = BigInt(value);
    return [valueDiff, scaleDiff, api.tk_flag_state(1, parseStatus, parsed, expectedFlags)];
  });
  if (!v.error_class && v.encode) encode(v, name, 0, Buffer.from(v.expect.values_hex, 'hex'),
    SIDE.has(name) ? [] : name === 'I2_S' ? [v.expect.scale_word] : v.expect.scale_words, v.data_hex);
}

function encode(v, name, expected, values, words, dataHex) {
  const format = formatId(name);
  const [, rows, cols, group] = geometry(v, name);
  const count = values.length;
  const zp = Buffer.from(v.zero_points_hex ?? '', 'hex');
  const capacity = Math.max(1, Number(api.tk_encoded_bytes(format, count, rows, cols, group)));
  const out = alloc(capacity), work = alloc(4 * count);
  const status = Number(api.tk_encode(format, bytesIn(values), values.length, count, rows, cols, group,
    wordsIn(words), words.length, bytesIn(zp), zp.length, work, count, out, capacity));
  judge(v, 'encode', expected, status, () => {
    const first = alloc(8);
    const diff = api.tk_compare_bytes(out, status, hexIn(dataHex), dataHex.length / 2, first);
    return [diff, 0n, 0];
  });
}

let containers = 0;
for (const family of ['bitnet_cpp', 'hf_bitnet', 'llama_cpp', 'mlx', 'onnx', 'prismml']) {
  const document = JSON.parse(readFileSync(new URL(`conformance/formats_${family}.json`, root), 'utf8'));
  const {errors, flags: slots} = document.constants;
  for (const v of document.vectors) {
    reset();
    const kind = v.kind === 'reject' ? v.reader : v.kind;
    if (kind in READERS) decode(v, READERS[kind] ?? v.format, errors, slots);
    else if (kind in WRITERS) {
      const name = WRITERS[kind] ?? v.format;
      const words = v.scale_words ?? (v.scale_word !== undefined ? [v.scale_word] : []);
      encode(v, name, errors[v.error_class], Buffer.from(v.values_hex, 'hex'), SIDE.has(name) ? [] : words, '');
    } else {
      assert.ok(kind === 'gguf' || kind === 'safetensors', `${v.id}: unknown reader ${kind}`);
      containers++;
    }
  }
}
assert.ok(calls > 0 && outcomes.rejected > 0 && outcomes.match > 0);
// The run verdict: this run passes; a run with no call, or with an idle
// filtered format, does not.
assert.equal(api.tk_run_passed(calls, 0, 0), 1);
assert.equal(api.tk_run_passed(0, 0, 0), 0);
assert.equal(api.tk_run_passed(calls, 0, 1), 0);
assert.equal(api.tk_run_passed(calls, 1, 0), 0);
const report = {calls, outcomes, container_vectors_outside_contract: containers};
writeFileSync(new URL('build/t27/ternary-contract-wasm-replay.json', root), JSON.stringify(report, null, 2) + '\n');
console.log(`PASS ternary contract in formats.wasm: ${calls} calls (${outcomes.match} match, ${outcomes.rejected} rejected); ` +
  `${containers} container vectors outside contract v1`);
