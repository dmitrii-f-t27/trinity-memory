import {createInspector} from './wasm-live.mjs';
import {readHeader} from './header-fetch.mjs';

self.onmessage = async ({data}) => {
  try {
    const response = await fetch('formats.wasm');
    if (!response.ok) throw new Error(`Cannot load the WASM parser (HTTP ${response.status})`);
    const {instance} = await WebAssembly.instantiate(await response.arrayBuffer(), {});
    const result = await readHeader(data.url, createInspector(instance.exports),
      progress => self.postMessage({type: 'progress', ...progress}));
    self.postMessage({type: 'result', result});
  } catch (error) {
    self.postMessage({type: 'error', message: error.message || String(error)});
  }
};
