"""Native live-parser answers for the WASM ABI parity test; no network."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tests.test_live import _file, _gguf, _hadamard_keys, _string
from trinity_memory import live

walk_fields = ('status reader needed version keys tensors data_start alignment ternary '
               'ternary_ok misfit_ternary misfit_other read record expected found').split()
cases = []


def add(name, header, size):
    rows = []
    for runtime in live.RUNTIMES:
        walk, _ = live.walk(header, size, runtime)
        fields = {key: (int(getattr(walk, key)) if key in ('status', 'reader')
                        else str(getattr(walk, key))) for key in walk_fields}
        model = None
        if walk.reader != live.TRUNCATED:
            value, run = live.Model([('test.gguf', header, size)]).verdict(runtime)
            model = {key: (int(getattr(run, key)) if key in ('status', 'verdict')
                           else str(getattr(run, key)))
                     for key in ('verdict', 'status', 'part', 'record', 'expected', 'found')}
            model['result'] = value
        rows.append({'id': runtime, 'walk': fields, 'model': model})
    cases.append({'name': name, 'header': header.hex(), 'fileSize': str(size), 'rows': rows})


for name, typ, group, width in [('q2', 42, 64, 18), ('prism', 142, 128, 34), ('legacy', 42, 128, 34)]:
    header, size = _file([('blk.0.ffn_down.weight', typ, group)], width)
    add(name, header, size)
    if name == 'q2':
        for length in (0, 4, 12, 23, 30, len(header) - 1):
            add(f'prefix-{length}', header[:length], size)
        add('file-short', header[:30], 30)
        add('large-file-size', header, 10 ** 17)
add('bad-magic', bytes(64), 1000000)
add('endian', bytes.fromhex('474755460000000300000000000000000000000000000000'), 1000000)
for block in (1024, 12):
    header, size = _file([('blk.0.attn_q.weight', 1, 1)], 2,
                         keys=_hadamard_keys(['blk.0.attn_q.weight'], block=block))
    add(f'rotation-{block}', header, size)
header, size = _file([('a', 42, 64)], 18, keys=[('longtext', 8, _string('x' * 300000))])
add('large-header', header, size)
print(json.dumps(cases))
