// This binding moves bytes and reads C fields; every verdict comes from t27.
export const RUNTIMES = [[1, 'llama.cpp'], [2, 'PrismML'], [3, 'bitnet.cpp'], [4, 'mortar.cpp'], [5, 'llama.cpp-94256114']];
export const TRUNCATED = -55;
const WALK = ['status', 'reader', 'needed', 'version', 'keys', 'tensors', 'data_start',
  'alignment', 'ternary', 'ternary_ok', 'misfit_ternary', 'misfit_other', 'read',
  'record', 'expected', 'found'];
const RUN = ['verdict', 'status', 'part', 'record', 'expected', 'found'];
const align = n => Math.ceil(n / 8) * 8;

export function createInspector(ex) {
  const base = align(Number(ex.__heap_base.value));
  const walkPtr = base, runPtr = align(walkPtr + ex.tlv_wasm_walk_size());
  const dataPtr = align(runPtr + ex.tlv_wasm_run_size());
  const limit = Number(ex.tlv_max_header());
  function fields(ptr, names, getter) {
    return Object.fromEntries(names.map((name, i) => [name,
      (name === 'status' || name === 'reader' || name === 'verdict')
        ? Number(BigInt.asIntN(64, getter(ptr, i))) : BigInt.asUintN(64, getter(ptr, i))]));
  }
  return {
    limit,
    inspect(bytes, fileSize) {
      fileSize = BigInt(fileSize);
      if (bytes.length > limit || fileSize < BigInt(bytes.length) || fileSize < 0n || fileSize > 0xffffffffffffffffn)
        throw new Error('Header or file size exceeds the supported bounds');
      const required = dataPtr + bytes.length;
      if (required > ex.memory.buffer.byteLength)
        ex.memory.grow(Math.ceil((required - ex.memory.buffer.byteLength) / 65536));
      new Uint8Array(ex.memory.buffer, dataPtr, bytes.length).set(bytes);
      const rows = RUNTIMES.map(([id, name]) => {
        ex.tlv_wasm_walk(dataPtr, bytes.length, fileSize, id, walkPtr);
        const walk = fields(walkPtr, WALK, ex.tlv_wasm_walk_field);
        let model = null;
        if (walk.reader !== TRUNCATED) {
          const result = ex.tlv_runtime(dataPtr, bytes.length, fileSize, id, runPtr);
          model = {...fields(runPtr, RUN, ex.tlv_wasm_run_field), result};
        }
        return {id, name, walk, model};
      });
      const incomplete = rows.filter(row => row.walk.reader === TRUNCATED);
      const needed = incomplete.reduce((n, row) => row.walk.needed > n ? row.walk.needed : n, 0n);
      return {rows, needed, complete: incomplete.length === 0, bytes: bytes.length, fileSize};
    },
  };
}
