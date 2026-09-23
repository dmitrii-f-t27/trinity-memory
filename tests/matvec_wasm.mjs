// Issue #33 through build/t27/formats.wasm, the WASM build of t27/matvec.t27,
// t27/formats.t27, t27/compute.t27 and t27/random.t27 (tools/build-t27-wasm.sh).
// Checks the CPython activation stream and a small product against an oracle
// written here, then, when build/fixtures holds the ranges, decodes every
// stored form of the real layers in the module and checks that its
// accumulators, partials and float steps hash to the committed report
// reports/ternary-check/matvec-2026-09-23.json. This script only reads files,
// moves bytes and compares digests. A form whose ranges are not cached is
// counted as skipped; with TRINITY_REQUIRE_CACHED=1 (the CI job
// ternary-check) any skip fails.
import {createHash} from 'node:crypto';
import {existsSync, readFileSync} from 'node:fs';
import assert from 'node:assert/strict';

const root = new URL('../', import.meta.url);
const binary = readFileSync(new URL('build/t27/formats.wasm', root));
const module = new WebAssembly.Module(binary);
assert.equal(WebAssembly.Module.imports(module).length, 0, 'formats.wasm must not import anything');
const {exports: api} = new WebAssembly.Instance(module, {});
const report = JSON.parse(readFileSync(new URL('reports/ternary-check/matvec-2026-09-23.json', root)));
const manifest = JSON.parse(readFileSync(new URL('fixtures/manifest.json', root)));

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
const view = (Type, at, n) => new Type(api.memory.buffer, at, n);
const sha = (at, bytes) => createHash('sha256').update(view(Uint8Array, at, bytes)).digest('hex');
const F16 = 1, BF16 = 2, F32 = 3;
const FORMAT = {PQ2_0: 5, PTQ1_0: 6, Q2_0: 3};
const GROUP = 64;

// ---- activation stream and a small product --------------------------------
reset();
const x = alloc(17408);
assert.equal(api.tmv_activations(27n, x, 17408), 0);
assert.equal(sha(x, 17408), report.activations.sha256_le, 'x = CPython random.Random(27).randint(-128, 127)');
assert.deepEqual([...view(Int8Array, x, 8)], report.activations.first);

{
  const rows = 3, cols = 192, groups = cols / GROUP;
  const w = alloc(4 * rows * cols), r64 = alloc(8 * cols), x64 = alloc(8 * cols);
  const p = alloc(8 * rows * groups), y = alloc(8 * rows);
  const weights = view(Int32Array, w, rows * cols);
  for (let i = 0; i < rows * cols; i++) weights[i] = ((i * 7919) % 3) - 1;
  assert.equal(api.tmv_matvec(w, rows, cols, x, GROUP, r64, x64, cols, p, rows * groups, y, rows), 0);
  const xs = view(Int8Array, x, cols);
  for (let r = 0; r < rows; r++) {
    let total = 0n;
    for (let j = 0; j < groups; j++) {
      let part = 0n;
      for (let k = j * GROUP; k < (j + 1) * GROUP; k++) part += BigInt(weights[r * cols + k] * xs[k]);
      assert.equal(view(BigInt64Array, p, rows * groups)[r * groups + j], part);
      total += part;
    }
    assert.equal(view(BigInt64Array, y, rows)[r], total);
  }
  weights[5] = 2;
  view(BigInt64Array, y, rows).fill(91n);
  assert.equal(api.tmv_matvec(w, rows, cols, x, GROUP, r64, x64, cols, p, rows * groups, y, rows), -53);
  assert.ok(view(BigInt64Array, y, rows).every((v) => v === 91n), 'a rejected product writes nothing');
}

// ---- real layers from the fixture cache -----------------------------------
function range(repo, tensor, kind, file = null) {
  const model = manifest.models.find((m) => m.repo === repo);
  const record = model.ranges.find((r) => r.tensor === tensor && r.kind === kind &&
    r.used_by.includes('layer0_matvec') && (file === null || r.file === file));
  const path = new URL(`build/fixtures/${repo.replace('/', '--')}/${model.revision}/${record.file}/${record.begin}-${record.end}.bin`, root);
  if (!existsSync(path)) return null;
  const data = readFileSync(path);
  assert.equal(createHash('sha256').update(data).digest('hex'), record.sha256, `${path.pathname}`);
  return {data, shape: record.shape};
}
function bytesIn(data) { const at = alloc(data.length); view(Uint8Array, at, data.length).set(data); return at; }
function wordsIn(data, width) {
  const n = data.length / width, at = alloc(4 * n), out = view(Uint32Array, at, n);
  for (let i = 0; i < n; i++) out[i] = width === 2 ? data.readUInt16LE(2 * i) : data.readUInt32LE(4 * i);
  return [at, n];
}
function product(values, rows, cols, xAt) {
  const groups = cols / GROUP;
  const r64 = alloc(8 * cols), x64 = alloc(8 * cols), p = alloc(8 * rows * groups), y = alloc(8 * rows);
  assert.equal(api.tmv_matvec(values, rows, cols, xAt, GROUP, r64, x64, cols, p, rows * groups, y, rows), 0);
  return {p, y, groups};
}
function checkIntegers(label, entry, rows, {p, y, groups}) {
  assert.equal(sha(y, 8 * rows), entry.accumulators.sha256_le, `${label} accumulators`);
  assert.deepEqual(Array.from(view(BigInt64Array, y, 8), Number), entry.accumulators.first);
  assert.equal(sha(p, 8 * rows * groups), entry.partials.sha256_le, `${label} partials`);
}

let replayed = 0, skipped = 0;
const bitnet = report.tensors.filter((t) => t.model === 'BitNet b1.58 2B4T');
for (const tensor of bitnet) {
  const [rows, cols] = tensor.tensor.shape, count = rows * cols;
  const packed = range('microsoft/bitnet-b1.58-2B-4T', tensor.tensor.hf, 'tensor');
  const scale = range('microsoft/bitnet-b1.58-2B-4T', `${tensor.tensor.hf}_scale`, 'scale');
  const i2s = range('microsoft/bitnet-b1.58-2B-4T-gguf', tensor.tensor.gguf, 'tensor');
  if (!packed || !scale || !i2s) { skipped += 2; continue; }  // hf_packed and I2_S
  const forms = [
    ['hf_packed', (values) => {
      assert.equal(api.tf_decode_hf_packed(bytesIn(packed.data), packed.data.length, rows, cols, values, count), 0n);
      return [scale.data.readUInt16LE(0), BF16];
    }],
    ['I2_S', (values) => {
      const word = alloc(4);
      assert.equal(api.tf_decode_i2s(bytesIn(i2s.data), i2s.data.length, count, values, count, word), 0n);
      return [view(Uint32Array, word, 1)[0], F32];
    }],
  ];
  for (const [label, decode] of forms) {
    reset();
    const xAt = alloc(cols);
    assert.equal(api.tmv_activations(27n, xAt, cols), 0);
    const values = alloc(4 * count);
    const [word, kind] = decode(values);
    const entry = tensor.formats[label];
    assert.equal(word, entry.float_step.scale.word, `${label} scale word`);
    const result = product(values, rows, cols, xAt);
    checkIntegers(`${tensor.tensor.hf} ${label}`, entry, rows, result);
    const yf = alloc(8 * rows);
    assert.equal(api.tmv_scale_tensor(result.y, rows, word, kind, yf, rows), 0);
    assert.equal(sha(yf, 8 * rows), entry.float_step.sha256_le, `${label} float step`);
    replayed++;
  }
}

const bonsai = report.tensors.find((t) => t.model === 'Ternary Bonsai 2 27B');
{
  const [rows, cols] = bonsai.tensor.shape, count = rows * cols;
  const files = {
    PTQ1_0: ['prism-ml/Ternary-Bonsai-2-27B-gguf', 'Ternary-Bonsai-2-27B-PTQ1_0.gguf'],
    PQ2_0: ['prism-ml/Ternary-Bonsai-2-27B-gguf', 'Ternary-Bonsai-2-27B-PQ2_0.gguf'],
    Q2_0: ['prism-ml/Ternary-Bonsai-2-27B-gguf-dev', 'Ternary-Bonsai-2-27B-Q2_0-prism-fork-required.gguf']};
  const forms = [];
  for (const label of ['PTQ1_0', 'PQ2_0', 'Q2_0']) {
    const [repo, file] = files[label];
    forms.push([label, () => range(repo, bonsai.tensor.gguf, 'tensor', file)?.data ?? null, (data, values) => {
      const per = api.tf_block_elements(FORMAT[label]), blocks = count / per, scales = alloc(4 * blocks);
      assert.equal(api.tf_decode_blocks(FORMAT[label], bytesIn(data), data.length, count, values, count, scales, blocks), 0n);
      return [scales, blocks, per];
    }]);
  }
  const mlx = 'prism-ml/Ternary-Bonsai-2-27B-mlx-2bit';
  forms.push(['mlx_2bit', () => range(mlx, `${bonsai.tensor.mlx}.weight`, 'tensor')?.data ?? null, (data, values) => {
    assert.equal(api.tf_decode_linear2(bytesIn(data), data.length, rows, cols, cols / 4, 1, values, count), 0n);
    const scaleRange = range(mlx, `${bonsai.tensor.mlx}.scales`, 'scale');
    const [scales, n] = wordsIn(scaleRange.data, 2);
    return [scales, n, count / n];
  }]);
  for (const [label, load, decode] of forms) {
    const data = load();
    if (!data) { skipped++; continue; }
    reset();
    const xAt = alloc(cols);
    assert.equal(api.tmv_activations(27n, xAt, cols), 0);
    const values = alloc(4 * count);
    const [scales, scaleCount, per] = decode(data, values);
    const entry = bonsai.formats[label];
    const result = product(values, rows, cols, xAt);
    checkIntegers(`${bonsai.tensor.gguf} ${label}`, entry, rows, result);
    const yf = alloc(8 * rows), exact = alloc(8 * rows), first = alloc(8);
    assert.equal(api.tmv_scale_groups(result.p, rows, result.groups, scales, scaleCount, F16, per / GROUP, yf, rows), 0);
    assert.equal(api.tmv_exact_groups_f16(result.p, rows, result.groups, scales, scaleCount, per / GROUP, exact, rows), 0);
    assert.equal(sha(yf, 8 * rows), entry.float_step.f64.sha256_le, `${label} f64 float step`);
    assert.equal(sha(exact, 8 * rows), entry.float_step.exact_times_2_24.sha256_le, `${label} exact form`);
    assert.equal(api.tmv_exact_mismatches(exact, 24, yf, rows, first), 0n);
    replayed++;
  }
}
const total = 2 * bitnet.length + Object.keys(bonsai.formats).length;
assert.equal(replayed + skipped, total, 'every stored form is either replayed or skipped');
console.log(`formats.wasm matvec: CPython activation stream and a small product checked; ` +
  `${replayed} of ${total} stored forms of the real layers reproduce reports/ternary-check/matvec-2026-09-23.json` +
  (skipped ? `, ${skipped} skipped (fixture ranges not cached: python3 tools/fetch-fixtures.py)` : ''));
if (process.env.TRINITY_REQUIRE_CACHED === '1' && skipped) {
  console.error(`TRINITY_REQUIRE_CACHED=1: ${skipped} stored forms were not recomputed`);
  process.exit(1);
}
