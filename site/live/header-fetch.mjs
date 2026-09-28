// Anonymous, bounded HTTP ranges. Never buffer a server's full-file 200 reply.
export function parseURL(input) {
  const url = new URL(input);
  if (url.protocol !== 'https:' || url.hostname !== 'huggingface.co' || url.port || url.username || url.password)
    throw new Error('Use a public HTTPS URL on huggingface.co');
  const parts = url.pathname.split('/').filter(Boolean);
  if (parts.length < 5 || parts[2] !== 'resolve' || !parts.at(-1).toLowerCase().endsWith('.gguf'))
    throw new Error('Use a GGUF /resolve/ URL');
  url.search = ''; url.hash = '';
  return {url, parts};
}

async function boundedBody(response, limit, exact = false) {
  if (!response.body) throw new Error('Empty HTTP response');
  const reader = response.body.getReader(), chunks = [];
  let length = 0;
  try {
    for (;;) {
      const {done, value} = await reader.read();
      if (done) break;
      length += value.byteLength;
      if (length > limit) throw new Error('Server sent more bytes than the bounded request');
      chunks.push(value);
    }
  } finally { await reader.cancel(); }
  if (exact && length !== limit) throw new Error('Truncated HTTP range response');
  const bytes = new Uint8Array(length);
  let offset = 0;
  for (const chunk of chunks) { bytes.set(chunk, offset); offset += chunk.length; }
  return bytes;
}

async function request(fetcher, url, options, read) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 30000);
  try {
    const response = await fetcher(url, {...options, credentials: 'omit', signal: controller.signal});
    return await read(response);
  } finally { clearTimeout(timer); controller.abort(); }
}

export async function readHeader(input, inspector, progress = () => {}, fetcher = fetch) {
  const {url, parts} = parseURL(input);
  if (!/^[a-f0-9]{40}$/.test(parts[3])) {
    const api = `https://huggingface.co/api/models/${parts[0]}/${parts[1]}/revision/${parts[3]}`;
    const info = await request(fetcher, api, {}, async response => {
      if (!response.ok) throw new Error(`Cannot resolve model revision (HTTP ${response.status})`);
      return JSON.parse(new TextDecoder().decode(await boundedBody(response, 8 * 1024 * 1024)));
    });
    if (!/^[a-f0-9]{40}$/.test(info.sha)) throw new Error('Hub did not return an immutable commit');
    parts[3] = info.sha;
    url.pathname = '/' + parts.join('/');
  }
  let bytes = new Uint8Array(), fileSize = null, etag = null, next = Math.min(262144, inspector.limit);
  for (;;) {
    const start = bytes.length;
    const addition = await request(fetcher, url.href, {headers: {Range: `bytes=${start}-${next - 1}`}}, async response => {
      if (response.status !== 206) {
        await response.body?.cancel();
        throw new Error(`Server must support bounded range reads (HTTP ${response.status}); full download cancelled`);
      }
      const range = /^bytes (\d+)-(\d+)\/(\d+)$/.exec(response.headers.get('Content-Range') || '');
      if (!range) throw new Error('Content-Range is unavailable; the server must expose it through CORS');
      const begin = BigInt(range[1]), end = BigInt(range[2]), total = BigInt(range[3]);
      if (begin !== BigInt(start) || end < begin || end >= BigInt(next) || end >= total)
        throw new Error('Server returned an inconsistent byte range');
      if (fileSize !== null && total !== fileSize) throw new Error('File size changed while reading');
      const currentTag = response.headers.get('ETag');
      if (etag && currentTag && etag !== currentTag) throw new Error('File identity changed while reading');
      fileSize = total; etag = currentTag || etag;
      return boundedBody(response, Number(end - begin + 1n), true);
    });
    const joined = new Uint8Array(bytes.length + addition.length);
    joined.set(bytes); joined.set(addition, bytes.length); bytes = joined;
    const result = inspector.inspect(bytes, fileSize);
    progress({bytes: bytes.length, limit: inspector.limit});
    if (result.complete) return {...result, url: url.href, revision: parts[3],
      split: /-\d{5}-of-\d{5}\.gguf$/i.test(parts.at(-1))};
    if (result.needed <= BigInt(bytes.length)) throw new Error('Parser requested no additional bytes');
    if (result.needed > BigInt(inspector.limit) || bytes.length >= inspector.limit)
      throw new Error(`Header exceeds the ${inspector.limit / 1048576} MiB inspection limit`);
    next = Math.min(inspector.limit, Math.max(bytes.length * 2, Number(result.needed)));
    if (BigInt(next) > fileSize) next = Number(fileSize);
    if (next <= bytes.length) throw new Error('File ends before the required header');
  }
}
