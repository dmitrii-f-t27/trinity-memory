"""Evidence claim prism_hadamard (issues #49 and #52).

counts(root) compiles t27/hadamard.t27 and t27/live.t27 (with their formats,
runtimes, json and codecs-free dependencies) to C with the pinned t27c, builds a
harness around the generated functions and runs, through them:

  - every vector of reports/prism_hadamard/inputs.json "rotation":
    sw_rotate_numerator for every output index against numerators computed by an
    independent butterfly in Python, sw_normalisation against an integer sqrt,
    the numerator divided by that divisor against the expected f64 bit pattern,
    and the inverse (rotating the numerators again, with the signs, returns
    block * input);
  - the "normalisation" table (a failure there aborts the claim);
  - the eight positive vectors of conformance/hadamard_rotate.json, bit for bit;
  - every GGUF header of "metadata" through tlv_hadamard, accepted or rejected
    with the status class the case was built to trigger.

It raises on the first mismatch, so a count below its constant means a case was
not run, never a case that was waved through.
"""
import json
import os
from pathlib import Path
import struct
import subprocess
import tempfile

HERE = Path(__file__).resolve()
INPUTS = 'reports/prism_hadamard/inputs.json'
FIXTURE = 'conformance/hadamard_rotate.json'
MODULES = ['json', 'formats', 'runtimes', 'live', 'hadamard']


def _compiler(root):
    base = Path(os.environ['T27_ROOT']).resolve()
    pin = (root / 'native/compiler.lock').read_text().strip()
    actual = subprocess.check_output(['git', '-C', str(base), 'rev-parse', 'HEAD'], text=True).strip()
    if actual != pin:
        raise ValueError('compiler pin mismatch')
    return base / 'target/release/t27c'


def _array(name, ctype, values):
    return f'static const {ctype} {name}[] = {{' + ','.join(str(v) for v in values) + '};\n'


def _source(root):
    inputs = json.loads((root / INPUTS).read_text())
    fixture = json.loads((root / FIXTURE).read_text())
    out = ['#include <stdint.h>\n#include <stdio.h>\n#include <string.h>\n#include "ffi.h"\n',
           '#include "json.h"\n#include "formats.h"\n#include "runtimes.h"\n#include "live.h"\n#include "hadamard.h"\n',
           'typedef struct { const char *id; uint32_t block; const int64_t *x; const int32_t *signs;'
           ' const int64_t *num; int64_t div; const uint64_t *bits; int has_num; } Rot;\n']
    rows = []
    for i, v in enumerate(inputs['rotation']):
        bits = struct.unpack(f'<{len(v["x"])}Q', bytes.fromhex(v['output_f64_le']))
        out += [_array(f'x{i}', 'int64_t', v['x']), _array(f's{i}', 'int32_t', v['signs']),
                _array(f'n{i}', 'int64_t', v['numerators']),
                _array(f'b{i}', 'uint64_t', [str(b) + 'ULL' for b in bits])]
        rows.append(f'{{"{v["id"]}",{v["block"]},x{i},s{i},n{i},{v["divisor"]},b{i},1}}')
    base = len(inputs['rotation'])
    positives = [v for v in fixture['vectors'] if 'expect_output' in v]
    for j, v in enumerate(positives):
        i = base + j
        block = v['block']
        x = [int(d) for d in struct.unpack(f'<{block}d', bytes.fromhex(v['values']))]
        if [float(a) for a in x] != list(struct.unpack(f'<{block}d', bytes.fromhex(v['values']))):
            raise ValueError('fixture input is not integral: ' + v['id'])
        signs = list(struct.unpack(f'<{block}i', bytes.fromhex(v['signs'])))
        bits = struct.unpack(f'<{block}Q', bytes.fromhex(v['expect_output']))
        out += [_array(f'x{i}', 'int64_t', x), _array(f's{i}', 'int32_t', signs),
                _array(f'b{i}', 'uint64_t', [str(b) + 'ULL' for b in bits])]
        rows.append(f'{{"{v["id"]}",{block},x{i},s{i},0,0,b{i},0}}')
    out.append('static const Rot rot[] = {' + ','.join(rows) + '};\n')
    out.append(f'enum {{ N_NEW = {base}, N_ALL = {base + len(positives)} }};\n')
    out.append(_array('norm_block', 'uint32_t', [v['block'] for v in inputs['normalisation']]))
    out.append(_array('norm_div', 'uint32_t', [v['divisor'] for v in inputs['normalisation']]))
    out.append(f'enum {{ N_NORM = {len(inputs["normalisation"])} }};\n')
    meta = []
    for i, v in enumerate(inputs['metadata']):
        raw = bytes.fromhex(v['header_hex'])
        out.append(_array(f'h{i}', 'uint8_t', list(raw)))
        meta.append(f'{{"{v["id"]}",h{i},{len(raw)},{v["file_size"]},{v["expected_status"]},{int(v["expected_present"])}}}')
    out.append('typedef struct { const char *id; const uint8_t *data; size_t size; uint64_t file; int32_t status; int present; } Meta;\n')
    out.append('static const Meta meta[] = {' + ','.join(meta) + '};\n')
    out.append(f'enum {{ N_META = {len(meta)} }};\n')
    out.append(r'''
static int rotate_new(const Rot *r) {
    int64_t y[1024];
    int64_t ones_back;
    int32_t ones[1024];
    if ((int64_t)sw_normalisation(r->block) != r->div) return 0;
    for (uint32_t k = 0; k < r->block; k++) {
        int64_t n = sw_rotate_numerator(k, r->block, (int64_t *)r->x, (int32_t *)r->signs);
        if (n != r->num[k]) return 0;
        double d = (double)n / (double)sw_normalisation(r->block);
        uint64_t bits; memcpy(&bits, &d, 8);
        if (bits != r->bits[k]) return 0;
        y[k] = n; ones[k] = 1;
    }
    for (uint32_t k = 0; k < r->block; k++) {
        ones_back = sw_rotate_numerator(k, r->block, y, ones) * (int64_t)r->signs[k];
        if (ones_back != (int64_t)r->block * r->x[k]) return 0;
    }
    return 1;
}
static int rotate_fixture(const Rot *r) {
    for (uint32_t k = 0; k < r->block; k++) {
        double d = (double)sw_rotate_numerator(k, r->block, (int64_t *)r->x, (int32_t *)r->signs)
                 / (double)sw_normalisation(r->block);
        uint64_t bits; memcpy(&bits, &d, 8);
        if (bits != r->bits[k]) return 0;
    }
    return 1;
}
int main(void) {
    int rot_ok = 0, fix_ok = 0, acc = 0, rej = 0, bad = 0;
    for (int i = 0; i < N_NORM; i++)
        if ((uint32_t)sw_normalisation(norm_block[i]) != norm_div[i]) { printf("MISMATCH norm %u\n", norm_block[i]); bad++; }
    for (int i = 0; i < N_NEW; i++) { if (rotate_new(&rot[i])) rot_ok++; else { printf("MISMATCH rotation %s\n", rot[i].id); bad++; } }
    for (int i = N_NEW; i < N_ALL; i++) { if (rotate_fixture(&rot[i])) fix_ok++; else { printf("MISMATCH fixture %s\n", rot[i].id); bad++; } }
    for (int i = 0; i < N_META; i++) {
        uint64_t row[5] = {0, meta[i].size, meta[i].file, 0, 0};
        TLVHadamard h;
        int32_t st = tlv_hadamard((uint8_t *)meta[i].data, meta[i].size, row, 1, &h);
        if (st == meta[i].status && (int)h.present == meta[i].present) {
            if (st == 0) acc++; else rej++;
        } else { printf("MISMATCH metadata %s got %d want %d\n", meta[i].id, (int)st, (int)meta[i].status); bad++; }
    }
    printf("COUNTS %d %d %d %d BAD %d\n", rot_ok, fix_ok, acc, rej, bad);
    return 0;
}
''')
    return ''.join(out)


def counts(root):
    root = Path(root)
    tool = _compiler(root)
    cc = os.environ.get('CC', 'cc')
    cxx = os.environ.get('CXX', 'c++')
    with tempfile.TemporaryDirectory(prefix='prism-hadamard-') as directory:
        work = Path(directory)
        for module in MODULES:
            header = subprocess.check_output([str(tool), 'gen-c', f't27/{module}.t27'], cwd=root, text=True)
            (work / f'{module}.h').write_text(header)
        (work / 'harness.c').write_text(_source(root))
        flags = ['-std=c11', '-O1', '-Wno-parentheses-equality', '-I', str(work), '-I', str(root / 'native')]
        subprocess.run([cc, *flags, '-c', str(work / 'harness.c'), '-o', str(work / 'harness.o')], check=True)
        subprocess.run([cxx, '-std=c++17', '-O1', '-c', str(root / 'native/float.cpp'), '-o', str(work / 'float.o')], check=True)
        subprocess.run([cxx, str(work / 'harness.o'), str(work / 'float.o'), '-o', str(work / 'harness')], check=True)
        result = subprocess.run([str(work / 'harness')], check=True, capture_output=True, text=True).stdout
    lines = result.strip().splitlines()
    summary = lines[-1].split()
    if summary[0] != 'COUNTS' or summary[-1] != '0':
        raise ValueError('prism_hadamard replay failed:\n' + result)
    return tuple(int(v) for v in summary[1:5])


CLAIM = {
    'name': 'prism_hadamard',
    'issues': [49, 52],
    'scope': 'Prism Hadamard rotation (t27/hadamard.t27) and prism.hadamard.* metadata checks '
             '(tlv_hadamard in t27/live.t27) run as generated C over committed vectors; no model file, '
             'runtime or hardware involved',
    'spec': 'specs/memory/prism_hadamard_evidence.t27',
    'accept': 'tmph_accept',
    'vectors': 'conformance/memory_prism_hadamard_evidence.json',
    'manifest': 'reports/prism_hadamard/evidence-manifest.json',
    # 80 rotation vectors, 8 fixture vectors, 18 accepted and 39 rejected headers; the same
    # numbers are the constants of the sealed spec.
    'expected': (80, 8, 18, 39),
    'bind': [
        'specs/memory/prism_hadamard_evidence.t27',
        'native/compiler.lock', 'native/float.cpp', 'native/ffi.h',
        't27/hadamard.t27', 't27/live.t27', 't27/formats.t27', 't27/runtimes.t27', 't27/json.t27',
        'reports/prism_hadamard/inputs.json', 'conformance/hadamard_rotate.json',
        'tools/generate-prism-hadamard-vectors.py', 'tools/evidence/prism_hadamard.py',
    ],
    'counts': counts,
}
