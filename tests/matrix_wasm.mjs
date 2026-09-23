// Issue #32 through build/t27/formats.wasm, the WASM build of t27/matrix.t27
// over t27/formats.t27 (tools/build-t27-wasm.sh). Checks exact scale words and
// a small round trip against oracles written here, then, when build/fixtures
// holds the ranges, recomputes every cell of the committed report
// reports/ternary-check.json in the module: published bytes decoded and
// compared with the reference, t27 round trips, representability reasons, the
// llama.cpp-encoded cells when build/upstream/matrix holds them
// (tests/upstream/run-llamacpp-matrix.sh), and the BitNet absmean cells with
// their tie counts. This script only reads files, moves bytes and compares.
// A cell whose bytes are not cached is counted as skipped; with
// TRINITY_REQUIRE_CACHED=1 (the CI job ternary-check) any skip fails, so all
// 36 cells and the derived ones must be recomputed.
import {createHash} from 'node:crypto';
import {existsSync, readFileSync} from 'node:fs';
import assert from 'node:assert/strict';

const root = new URL('../', import.meta.url);
const binary = readFileSync(new URL('build/t27/formats.wasm', root));
const module = new WebAssembly.Module(binary);
assert.equal(WebAssembly.Module.imports(module).length, 0, 'formats.wasm must not import anything');
const {exports: api} = new WebAssembly.Instance(module, {});
const report = JSON.parse(readFileSync(new URL('reports/ternary-check.json', root)));
const manifest = JSON.parse(readFileSync(new URL('fixtures/manifest.json', root)));

const heapBase = Number(api.__heap_base.value);
let top = heapBase;
const mark = () => top;
const release = (at) => { top = at; };
function alloc(bytes) {
  const at = (top + 15) & ~15;
  top = at + Math.max(bytes, 1);
  const missing = top - api.memory.buffer.byteLength;
  if (missing > 0) api.memory.grow(Math.ceil(missing / 65536));
  return at;
}
const view = (Type, at, n) => new Type(api.memory.buffer, at, n);
const sha = (at, bytes) => createHash('sha256').update(view(Uint8Array, at, bytes)).digest('hex');
const KIND = {F16: 1, BF16: 2, F32: 3};
const R = {status: 0, trits_differ: 1, trits_first: 2, trits_explain: 3, scales_differ: 4, scales_first: 5,
  scales_explain: 6, reason: 7, outside: 8, code_bytes: 9, stored_bytes: 10};
const STATUS = Object.fromEntries(Object.entries(report.taxonomy.cell_status).map(([k, v]) => [v, k]));
const EXPLAIN = {0: null, ...Object.fromEntries(Object.entries(report.taxonomy.explanations).map(([k, v]) => [v.code, k]))};
const REASONS = Object.entries(report.taxonomy.reasons).sort((a, b) => a[1].code - b[1].code).map(([k]) => k);
const slot = (at, i) => Number(view(BigInt64Array, at, R.stored_bytes + 1)[i]);
const EXPLAIN_CODE = Object.fromEntries(Object.entries(report.taxonomy.explanations).map(([k, v]) => [k, v.code]));
const requireCached = process.env.TRINITY_REQUIRE_CACHED === '1';

// ---- oracles ---------------------------------------------------------------
{
  const f32 = new Float32Array(1), u32 = new Uint32Array(f32.buffer);
  let state = 32;
  for (let i = 0; i < 20000; i++) {
    state ^= state << 13; state ^= state >>> 17; state ^= state << 5;
    u32[0] = (state >>> 0) % 0x7f000000;
    if (i % 4 === 0) u32[0] &= 0xffff0000;
    assert.equal(Number(api.tmx_float_bits(f32[0], KIND.F32)), u32[0] === 0 ? 0 : u32[0]);
    if ((u32[0] & 0xffff) === 0) assert.equal(Number(api.tmx_float_bits(f32[0], KIND.BF16)), u32[0] >>> 16);
    else assert.equal(Number(api.tmx_float_bits(f32[0], KIND.BF16)), -1);
  }
  assert.equal(Number(api.tmx_float_bits(1.21875, KIND.F16)), 0x3ce0);
  assert.equal(api.tmx_bf16_round(0x3f9c036f), 0x3f9c);
  // A 4 x 256 ternary tensor, one bf16 scale: every format but Q1_0 holds it.
  const rows = 4, cols = 256, n = rows * cols;
  const t = alloc(4 * n), s = alloc(4), words = alloc(4 * n), out = alloc(n), back = alloc(4 * n);
  const backWords = alloc(4 * n), reasons = alloc(80), result = alloc(8 * 11);
  view(Int32Array, t, n).forEach((_, i, a) => { a[i] = ((i * 7919) % 3) - 1; });
  view(Uint32Array, s, 1)[0] = 0x3f9c;
  for (const [fmt, group] of [[1, 0], [2, 0], [3, 0], [5, 0], [6, 0], [7, 0], [8, 0], [9, 128], [10, 128]]) {
    assert.equal(api.tmx_round_trip(fmt, rows, cols, group, t, s, 1, KIND.BF16, 0, words, n, out, n, back, n,
      backWords, reasons, result), 0);
    assert.equal(slot(result, R.status), 0, `format ${fmt}`);
    assert.deepEqual([...view(Int32Array, back, n)], [...view(Int32Array, t, n)]);
  }
  assert.equal(api.tmx_round_trip(4, rows, cols, 0, t, s, 1, KIND.BF16, 0, words, n, out, n, back, n,
    backWords, reasons, result), 0);
  assert.equal(slot(result, R.status), 2);
  assert.equal(slot(result, R.reason), REASONS.indexOf('binary_only'));
  release(heapBase);
}
{
  // Ties: bf16 master words at +-t = 0.5 x weight_scale (0x3f1c for 0x3f9c) every other weight, 1.0 elsewhere.
  // The derived trits are 0 at +-t. A reference that stores both 0 and +-1 at one tie value is a split;
  // one consistent rule (every tie +-1, or every tie 0 against derived +-1) is not, and stays unexplained.
  const rows = 4, cols = 16, n = rows * cols, scale = 0x3f9c, tie = 0x3f1c;
  const master = alloc(2 * n), ref = alloc(4 * n), der = alloc(4 * n), detail = alloc(8 * 9), result = alloc(8 * 11);
  const words = view(Uint16Array, master, n), r = view(Int32Array, ref, n), d = view(Int32Array, der, n);
  for (let i = 0; i < n; i++) words[i] = i % 4 === 0 ? tie : i % 4 === 1 ? tie | 0x8000 : 0x3f80;
  const sign = (i) => (words[i] & 0x8000 ? -1 : 1);
  const atTie = (i) => (words[i] & 0x7fff) === tie;
  const explain = (refAt, derAt) => {
    for (let i = 0; i < n; i++) { r[i] = atTie(i) ? refAt(i) : 1; d[i] = atTie(i) ? derAt(i) : 1; }
    assert.equal(api.tmx_compare(ref, der, rows, cols, 0, 0, 0, 0, 0, 0, 0, 0, 0, result), 0);
    assert.equal(api.tmx_explain_ties(master, 2 * n, n, scale, ref, der, detail, result), 0);
    assert.equal(Number(view(BigInt64Array, detail, 9)[6]), 0, 'no difference away from the tie value');
    return EXPLAIN[slot(result, R.trits_explain)];
  };
  assert.equal(explain((i) => (i % 8 < 4 ? sign(i) : 0), () => 0), 'tie_split');
  assert.equal(explain((i) => sign(i), () => 0), 'unexplained');
  assert.equal(explain(() => 0, (i) => sign(i)), 'unexplained');
  // Scales differing only where the trit is 0 in both tensors are explained; a +1 on one side is not.
  const zr = 2, zc = 64, zn = zr * zc, groups = zn / 32;
  const a = alloc(4 * zn), b = alloc(4 * zn), full = alloc(4 * groups), zeroed = alloc(4 * groups);
  view(Int32Array, a, zn).forEach((_, i, t) => { t[i] = i >= 32 && i < 64 ? 0 : ((i * 7919) % 3) - 1; });
  view(Uint32Array, full, groups).fill(0x3ce0);
  view(Uint32Array, zeroed, groups).fill(0x3ce0);
  view(Uint32Array, zeroed, groups)[1] = 0;
  const compareScales = (x, y) => {
    assert.equal(api.tmx_compare(x, y, zr, zc, full, groups, KIND.F16, 32, zeroed, groups, KIND.F16, 32, 1, result), 0);
    return [slot(result, R.scales_differ), EXPLAIN[slot(result, R.scales_explain)]];
  };
  view(Int32Array, b, zn).set(view(Int32Array, a, zn));
  assert.deepEqual(compareScales(a, b), [32, 'scale_zero_weights']);
  view(Int32Array, b, zn)[40] = 1;
  assert.deepEqual(compareScales(a, b), [32, 'unexplained']);
  assert.deepEqual(compareScales(b, a), [32, 'unexplained']);
  // Bits per weight and the GGUF metadata share (the spec's case: 21-byte name, 2 dims, alignment 32).
  assert.equal(api.tmx_bits_per_weight(1638432n, 6553600n), 2.0000390625);
  assert.equal(api.tmx_gguf_metadata_bytes(21n, 2n, 25067520n, 32n), 61n);
  assert.equal(api.tmx_gguf_metadata_bytes(21n, 2n, 25067521n, 32n), 92n);
  assert.equal(api.tmx_gguf_metadata_bytes(21n, 2n, 1n, 24n), -50n);
  assert.ok(EXPLAIN_CODE.tie_split && EXPLAIN_CODE.scale_zero_weights);
  release(heapBase);
}

// ---- the committed report --------------------------------------------------
function cached(source) {
  const model = manifest.models.find((m) => m.repo === source.repo);
  const record = model.ranges.find((r) => r.file === source.file && r.begin === source.range[0] && r.end === source.range[1]);
  assert.ok(record, `${source.repo} ${source.file} ${source.range} is not in fixtures/manifest.json`);
  const path = new URL(`build/fixtures/${source.repo.replace('/', '--')}/${model.revision}/${source.file}/${record.begin}-${record.end}.bin`, root);
  if (!existsSync(path)) return null;
  const data = readFileSync(path);
  assert.equal(createHash('sha256').update(data).digest('hex'), record.sha256, path.pathname);
  return data;
}
function bytesIn(data) { const at = alloc(data.length); view(Uint8Array, at, data.length).set(data); return at; }
function wordsIn(data, width) {
  const n = data.length / width, at = alloc(4 * n), out = view(Uint32Array, at, n);
  for (let i = 0; i < n; i++) out[i] = width === 2 ? data.readUInt16LE(2 * i) : data.readUInt32LE(4 * i);
  return [at, n];
}
const formatOf = Object.fromEntries(report.formats.map((f) => [f.id, f]));

// Decodes third-party bytes of one cell: {values, words, n, kind, group, code: [at, size]} or null (not cached).
function decode(tensor, cell) {
  const [rows, cols] = tensor.shape, count = rows * cols, fmt = formatOf[cell.format].t27_format;
  const src = cell.source[0];
  let data;
  if (src.upstream) {
    const path = new URL(src.file, root);
    if (!existsSync(path)) return null;
    data = readFileSync(path);
  } else {
    data = cached(src);
  }
  if (!data) return null;
  const at = bytesIn(data), values = alloc(4 * count);
  if (fmt === 8) {
    const scale = cached(cell.source[1]);
    assert.equal(api.tf_decode_hf_packed(at, data.length, rows, cols, values, count), 0n);
    const [w, n] = wordsIn(scale, 2);
    return {values, words: w, n, kind: KIND.BF16, group: 0, code: [at, data.length]};
  }
  if (fmt === 7) {
    const w = alloc(4);
    assert.equal(api.tf_decode_i2s(at, data.length, count, values, count, w), 0n);
    return {values, words: w, n: 1, kind: KIND.F32, group: 0, code: [at, data.length]};
  }
  if (fmt === 9) {
    const [w, n] = wordsIn(cached(cell.source[1]), 2);
    assert.equal(api.tf_decode_mlx2(at, data.length, rows, cols, count / n, values, count), 0n);
    return {values, words: w, n, kind: KIND.F16, group: count / n, code: [at, data.length]};
  }
  const per = api.tf_block_elements(fmt), blocks = count / per, w = alloc(4 * blocks);
  assert.equal(api.tf_decode_blocks(fmt, at, data.length, count, values, count, w, blocks), 0n);
  return {values, words: w, n: blocks, kind: KIND.F16, group: per, code: [at, data.length]};
}

function checkCompared(cell, result) {
  assert.equal(STATUS[slot(result, R.status)], cell.status, cell.id);
  assert.equal(slot(result, R.trits_differ), cell.trits.differ, `${cell.id} trits`);
  assert.equal(slot(result, R.trits_first), cell.trits.first, `${cell.id} first trit`);
  assert.equal(EXPLAIN[slot(result, R.trits_explain)], cell.trits.explanation, `${cell.id} trit explanation`);
  if (cell.scales.compared === false) return;
  assert.equal(slot(result, R.scales_differ), cell.scales.differ, `${cell.id} scales`);
  assert.equal(slot(result, R.scales_first), cell.scales.first, `${cell.id} first scale`);
  assert.equal(EXPLAIN[slot(result, R.scales_explain)], cell.scales.explanation, `${cell.id} scale explanation`);
}
function checkBits(cell, count) {
  assert.equal(api.tmx_bits_per_weight(BigInt(cell.stored_bytes), BigInt(count)), cell.bits_per_weight, `${cell.id} bits`);
  const m = cell.metadata;
  const extra = m.record ? Number(api.tmx_gguf_metadata_bytes(BigInt(m.record.name_size), BigInt(m.record.dims),
    BigInt(cell.stored_bytes), BigInt(m.record.alignment))) : 0;
  assert.equal(extra, m.bytes, `${cell.id} metadata bytes`);
  assert.equal(api.tmx_bits_per_weight(BigInt(cell.stored_bytes + extra), BigInt(count)), m.bits_per_weight,
    `${cell.id} bits with metadata`);
}
function checkReasons(cell, reasons) {
  const got = [];
  const all = view(BigInt64Array, reasons, 2 * REASONS.length);
  REASONS.forEach((token, i) => { if (all[2 * i] > 0n) got.push([token, Number(all[2 * i]), Number(all[2 * i + 1])]); });
  assert.deepEqual(got, cell.reasons.map((r) => [r.reason, r.count, r.first]), `${cell.id} reasons`);
}

let replayed = 0, skipped = 0;
for (const tensor of report.tensors) {
  release(heapBase);
  const [rows, cols] = tensor.shape, count = rows * cols;
  const cells = report.cells.filter((c) => c.tensor === tensor.id);
  const refCell = cells.find((c) => c.provenance === 'reference');
  const derivedCells = report.derived.filter((c) => c.tensor === tensor.id);
  const ref = decode(tensor, refCell);
  if (!ref) { skipped += cells.length + derivedCells.length; continue; }
  assert.equal(sha(ref.values, 4 * count), tensor.reference.trits_sha256_le, `${tensor.id} reference trits`);
  const base = mark();
  const result = alloc(8 * 11), reasons = alloc(16 * REASONS.length), first = alloc(8);
  const afterRef = mark();
  for (const cell of cells) {
    release(afterRef);
    const format = formatOf[cell.format], fmt = format.t27_format, group = format.group_argument;
    if (cell.status !== 'not-representable') checkBits(cell, count);
    // The columns the t27 writer fills: a round trip, or not representable and never written.
    if (format.writer === 't27' && !['reference', 'published'].includes(cell.provenance)) {
      const n = api.tmx_scale_count(rows, cols, api.tmx_scale_group(fmt, group));
      const words = alloc(4 * n), cap = Math.floor(count / 2) + rows * Math.max(group, 64) + 64, out = alloc(cap);
      const back = alloc(4 * count), backWords = alloc(4 * n);
      assert.equal(api.tmx_round_trip(fmt, rows, cols, group, ref.values, ref.words, ref.n, ref.kind, ref.group,
        words, n, out, cap, back, count, backWords, reasons, result), 0, cell.id);
      assert.equal(STATUS[slot(result, R.status)], cell.status, cell.id);
      if (cell.status === 'not-representable') checkReasons(cell, reasons);
      else {
        checkCompared(cell, result);
        assert.equal(sha(out, slot(result, R.code_bytes)), cell.bytes_sha256, `${cell.id} bytes`);
        assert.equal(slot(result, R.stored_bytes), cell.stored_bytes, `${cell.id} stored bytes`);
      }
      replayed++;
      continue;
    }
    if (cell.status === 'not-representable') {
      const primary = Number(api.tmx_representable(fmt, rows, cols, group, ref.values, ref.words, ref.n, ref.kind,
        ref.group, reasons));
      assert.equal(REASONS[primary], cell.reasons[0].reason, cell.id);
      checkReasons(cell, reasons);
      replayed++;
      continue;
    }
    const stored = decode(tensor, cell);
    if (!stored) { skipped++; continue; }
    assert.equal(api.tmx_compare(ref.values, stored.values, rows, cols, ref.words, ref.n, ref.kind, ref.group,
      stored.words, stored.n, stored.kind, stored.group, 1, result), 0, cell.id);
    checkCompared(cell, result);
    const scratch = alloc(stored.code[1]);
    const differ = api.tmx_reencode(fmt, rows, cols, group, stored.values, stored.words, stored.n, stored.code[0],
      stored.code[1], scratch, stored.code[1], first);
    assert.equal(Number(differ), cell.t27_reencode.differ_bytes, `${cell.id} re-encode`);
    replayed++;
  }
  for (const cell of derivedCells) {
    release(afterRef);
    const master = cached(cell.source);
    if (!master) { skipped++; continue; }
    const at = bytesIn(master), values = alloc(4 * count), stats = alloc(16), detail = alloc(8 * 9);
    assert.equal(Number(api.tf_absmean_bf16(at, count, 1e-5, values, count, stats)), cell.boundary_weights);
    assert.equal(view(Float64Array, stats, 2)[0], cell.scales.mean_abs);
    assert.equal(api.tmx_compare(ref.values, values, rows, cols, ref.words, ref.n, ref.kind, ref.group,
      ref.words, ref.n, ref.kind, ref.group, 0, result), 0);
    assert.equal(api.tmx_explain_ties(at, master.length, count, view(Uint32Array, ref.words, 1)[0], ref.values, values,
      detail, result), 0);
    checkCompared(cell, result);
    const d = Array.from(view(BigInt64Array, detail, 9), Number);
    const ties = cell.ties;
    assert.deepEqual(d, [ties.tie_word, ties.positive.packed_nonzero, ties.positive.packed_zero,
      ties.negative.packed_nonzero, ties.negative.packed_zero, ties.differ_at_ties, ties.differ_elsewhere,
      ties.first_elsewhere, ties.derived_nonzero_at_ties], `${cell.id} ties`);
    replayed++;
  }
  release(base);
}
const total = report.cells.length + report.derived.length;
assert.equal(replayed + skipped, total, 'every cell is either replayed or skipped');
console.log(`formats.wasm matrix: exact scale words, round trips of 10 formats, tie and zero-weight explanations ` +
  `checked; ${replayed} of ${total} cells of reports/ternary-check.json reproduce` +
  (skipped ? `, ${skipped} skipped (fixture ranges or llama.cpp-encoded tensors not cached: python3 ` +
    `tools/fetch-fixtures.py, sh tests/upstream/run-llamacpp-matrix.sh)` : ''));
if (requireCached && skipped) {
  console.error(`TRINITY_REQUIRE_CACHED=1: ${skipped} cells were not recomputed`);
  process.exit(1);
}
