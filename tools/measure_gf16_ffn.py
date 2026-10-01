"""Measure full/result-mode GF16 clocks against the same saved integer oracle."""
import argparse
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools import gf16_ffn_build as build, gf16_ffn_vectors as vectors

SOURCES = ('t27/rtl/gf16_ffn.t27', 't27/rtl/gf16_scalar.t27',
           't27/rtl/gf16_wide_norm.t27', 't27/rtl/ffn_wide.t27',
           'rtl/t27/gf16_ffn.v', 'rtl/t27/gf16_wide_norm.v',
           'tests/tb_gf16_ffn.v', 'tools/gf16_ffn_build.py',
           'tools/gf16_ffn_vectors.py', 'tools/gf16_wide_build.py',
           'tools/measure_gf16_ffn.py', 'native/compiler.lock')


def hashes():
    return {p: hashlib.sha256((ROOT/p).read_bytes()).hexdigest() for p in SOURCES}


def measure(folder, work, out_base=1):
    folder = Path(folder).resolve(); work = Path(work).resolve()
    reference = json.loads((folder/'reference.json').read_text())
    if reference.get('profile') != 'gf16-ffn-v1':
        raise ValueError('GF16 reference required')
    run = reference.get('run', 1)
    manifest = json.loads((folder/'inputs.json').read_text())
    vectors.validate_board_inputs(folder, manifest, reference, run)
    work.mkdir(parents=True, exist_ok=False)
    sources = hashes()
    rtl, _ = build.generate(work/'generated')
    shape = tuple(len(reference['stages'][s]) for s in ('h', 'g', 'y'))
    command = build.compile_sim(work/'sim', rtl, shape, 'verilator', out_base=out_base)
    # Build a fresh memory file from the independently hashed binary regions;
    # do not trust an old input.mem alongside them. Only the descriptor changes.
    results = {}
    for mode in ('full', 'result'):
        trial = work/mode; trial.mkdir()
        inp = trial/'input.mem'
        with inp.open('w') as f:
            for name, region in manifest.items():
                data = (folder/region['file']).read_bytes()
                if hashlib.sha256(data).hexdigest() != region['sha256']:
                    raise ValueError('input payload changed during measurement: '+name)
                if name == 'doorbell':
                    data = (run | (vectors.MAGIC << 32) | (vectors.trace_flag(mode) << 64)).to_bytes(16, 'little')
                f.write(f'@{region["word_address"]:x}\n')
                f.writelines(f'{int.from_bytes(data[i:i+16], "little"):032x}\n' for i in range(0,len(data),16))
        capture = trial/'capture.txt'
        log = build.base.run([*command, '+input='+str(inp), '+output='+str(capture)], timeout=3600)
        (trial/'simulation.log').write_text(log)
        result = vectors.validate(capture.read_bytes(), {**reference, 'trace':mode}, run)
        results[mode] = result
        (trial/'result.json').write_text(json.dumps(result, indent=2)+'\n')
        print(mode, json.dumps(result), flush=True)
    if hashes() != sources:
        raise ValueError('sources changed during measurement')
    report = {'schema':'trinity.gf16-ffn-performance.v1', 'kind':'simulation',
              'sources_sha256':sources, 'reference_sha256':hashlib.sha256((folder/'reference.json').read_bytes()).hexdigest(),
              'input_payloads':manifest, 'shape':shape, 'mem_base':2, 'out_base':out_base,
              'simulator':build.base.run(['verilator','--version']).strip(), 'runs':results}
    (work/'result.json').write_text(json.dumps(report, indent=2)+'\n')
    return report


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--vectors', type=Path, required=True)
    ap.add_argument('--output', type=Path, required=True)
    ap.add_argument('--out-base', type=int, default=1)
    args = ap.parse_args()
    if args.out_base < 1:ap.error('--out-base must be positive')
    measure(args.vectors, args.output, args.out_base)


if __name__ == '__main__':
    main()
