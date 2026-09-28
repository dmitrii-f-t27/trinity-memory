import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {execFileSync} from 'node:child_process';
import {createInspector} from '../site/live/wasm-live.mjs';
import {parseURL, readHeader} from '../site/live/header-fetch.mjs';

const {instance} = await WebAssembly.instantiate(readFileSync('build/t27/formats.wasm'), {});
const inspector = createInspector(instance.exports);
const vectors = JSON.parse(execFileSync(process.env.PYTHON || 'python3', ['tests/live_wasm_vectors.py'],
  {maxBuffer: 10 * 1024 * 1024, encoding: 'utf8'}));
const normalize = value => JSON.parse(JSON.stringify(value, (_, v) => typeof v === 'bigint' ? v.toString() : v));
for (const vector of vectors) {
  const result = inspector.inspect(Buffer.from(vector.header, 'hex'), BigInt(vector.fileSize));
  assert.deepEqual(normalize(result.rows.map(({id, walk, model}) => ({id, walk, model}))), vector.rows, vector.name);
}
assert.equal(inspector.inspect(Buffer.from(vectors.find(v => v.name === 'prism').header, 'hex'),
  BigInt(vectors.find(v => v.name === 'prism').fileSize)).rows[1].model.verdict, 0);

assert.throws(() => inspector.inspect(new Uint8Array(), 1n << 64n), /bounds/);

const sha = '0123456789abcdef0123456789abcdef01234567';
const url = `https://huggingface.co/owner/model/resolve/${sha}/model.gguf`;
const sample = vectors.find(v => v.name === 'large-header');
const whole = Buffer.alloc(Number(sample.fileSize));
Buffer.from(sample.header, 'hex').copy(whole);
const ranges = [];
async function rangeFetch(requestURL, options) {
  assert.equal(options.credentials, 'omit');
  assert.equal(requestURL, url);
  const [, startText, endText] = /^bytes=(\d+)-(\d+)$/.exec(options.headers.Range);
  const start = Number(startText), end = Math.min(Number(endText), whole.length - 1);
  ranges.push([start, end]);
  return new Response(whole.subarray(start, end + 1), {status: 206,
    headers: {'Content-Range': `bytes ${start}-${end}/${whole.length}`, ETag: 'same-file'}});
}
const result = await readHeader(url, inspector, () => {}, rangeFetch);
assert.equal(result.complete, true);
assert.equal(ranges.length, 2);
assert.equal(ranges[1][0], ranges[0][1] + 1, 'ranges append without downloading the prefix twice');
assert.equal(result.rows[1].model.verdict, 0);
assert.throws(() => parseURL('https://example.org/a.gguf'), /huggingface/);
assert.throws(() => parseURL('https://secret@huggingface.co/owner/model/resolve/main/a.gguf'), /public/);
let cancelled = false;
await assert.rejects(readHeader(url, inspector, () => {}, async () => new Response(new ReadableStream({
  cancel() { cancelled = true; },
}), {status: 200})), /full download cancelled/);
assert.equal(cancelled, true);
await assert.rejects(readHeader(url, inspector, () => {}, async () => new Response('x', {status: 206,
  headers: {'Content-Range': 'bytes 1-1/10'}})), /inconsistent/);
await assert.rejects(readHeader(url, inspector, () => {}, async () => new Response('xx', {status: 206,
  headers: {'Content-Range': 'bytes 0-0/10'}})), /more bytes/);
let resolved = false;
await readHeader(url.replace(sha, 'main'), inspector, () => {}, async (requestURL, options) => {
  if (requestURL.includes('/api/models/')) { resolved = true; return Response.json({sha}); }
  return rangeFetch(requestURL, options);
});
assert.equal(resolved, true);
console.log(`PASS live formats.wasm: ${vectors.length} headers x 3 runtimes, native parity and bounded HTTP ranges`);
