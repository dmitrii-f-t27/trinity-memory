const state = document.getElementById('state'), out = document.getElementById('out');
const go = document.getElementById('go'), cancel = document.getElementById('cancel');
const statuses = new Map([
  [0, 'OK'], [-55, 'Header incomplete'], [-63, 'Hadamard key type'], [-64, 'Missing Hadamard key'],
  [-65, 'Hadamard version'], [-66, 'Hadamard block size'], [-67, 'Hadamard transform'],
  [-68, 'Hadamard signs'], [-69, 'Hadamard architecture'], [-70, 'Hadamard tensor name'],
  [-71, 'Hadamard tensor'], [-72, 'Tensor offsets'], [-73, 'GGUF magic'], [-74, 'GGUF version'],
  [-75, 'Endianness'], [-76, 'Metadata key'], [-77, 'Alignment'], [-78, 'Tensor name'],
  [-79, 'Tensor shape'], [-80, 'Tensor type'], [-81, 'Row alignment'], [-82, 'File bounds'],
  [-83, 'Architecture'], [-84, 'Inspection limit'], [-85, 'Split metadata'], [-86, 'Record count'],
  [-87, 'File ends inside its header'],
  [-88, 'Hadamard tied output metadata'], [-89, 'Tensor activation precision metadata'],
]);
let worker;
const node = (tag, text) => { const element = document.createElement(tag); element.textContent = text; return element; };
const statusText = code => statuses.get(code) || `Status ${code}`;
function stop() { worker?.terminate(); worker = null; go.disabled = false; cancel.hidden = true; }
function render(result) {
  const pinned = node('a', `Pinned file · ${result.revision.slice(0, 12)}`);
  pinned.href = result.url; pinned.rel = 'noopener'; pinned.target = '_blank'; out.append(pinned);
  out.append(node('p', `${result.bytes.toLocaleString()} bytes read of ${result.fileSize.toLocaleString()} bytes.`));
  const table = document.createElement('table'), head = document.createElement('thead');
  const headings = document.createElement('tr');
  for (const title of ['Runtime', 'Header', 'Model metadata', 'Ternary records']) headings.append(node('th', title));
  head.append(headings); table.append(head);
  const body = document.createElement('tbody');
  for (const {name, walk, model} of result.rows) {
    const tr = document.createElement('tr');
    const verdict = result.split ? 'Not checked across split parts' : !model || model.result < 0
      ? `Undecided: ${statusText(model?.status ?? walk.reader)}`
      : model.result === 0 ? 'Accepts' : model.result === 2 ? 'Accepts; rotation ignored'
      : `Refuses: ${statusText(model.status)}`;
    for (const value of [name, statusText(walk.status), verdict,
      `${walk.ternary_ok} fitting / ${walk.ternary} encountered`]) tr.append(node('td', value));
    body.append(tr);
  }
  table.append(body); out.append(table);
  out.append(node('p', 'These answers use the runtime revisions pinned in this build; they are not a claim about every runtime version.'));
}
cancel.onclick = () => { stop(); state.textContent = 'Cancelled.'; };
document.getElementById('check-form').onsubmit = event => {
  event.preventDefault(); stop(); out.replaceChildren(); state.className = '';
  state.textContent = 'Resolving the file and loading the local parser…'; go.disabled = true; cancel.hidden = false;
  worker = new Worker(new URL('./check-worker.mjs', import.meta.url), {type: 'module'});
  worker.onmessage = ({data}) => {
    if (data.type === 'progress') { state.textContent = `Reading the header… ${data.bytes.toLocaleString()} bytes`; return; }
    stop();
    if (data.type === 'error') { state.className = 'bad'; state.textContent = data.message; return; }
    render(data.result); state.textContent = 'Done — computed locally with WebAssembly.';
  };
  worker.onerror = event => { stop(); state.className = 'bad'; state.textContent = event.message || 'The parser worker could not start.'; };
  worker.postMessage({url: document.getElementById('url').value.trim()});
};
