"""Evidence claim for issue #113: exact GF16 FFN projection speed-up.

Replays retained captures only (reports/fpga/gf16-performance-2026-10-01/); it
never touches a board or the network. counts(root) first runs the two offline
verifiers that ship with the evidence, then recomputes every count itself from
the retained raw UART captures with tools.gf16_ffn_vectors.validate (the same
strict validator the verifiers use): word/code comparison against the saved
integer oracle, disjoint phase partition, loop-clock count.

Not runnable here: verify-raw-board.py (needs the local DDR payloads and raw
readback streams under build/gf16-performance/, which are not committed). Its
sha256 is bound, and verify-board-evidence.py checks the receipt it produced.
"""
import gzip
import json
from pathlib import Path
import subprocess
import sys

EVIDENCE = 'reports/fpga/gf16-performance-2026-10-01'
CLOCK_HZ = 60000000


def _read(folder, name):
    path = folder / name
    return path.read_bytes() if path.exists() else gzip.decompress((folder / (name + '.gz')).read_bytes())


def _ppm(old, new):
    if not old > new > 0:
        raise ValueError('accelerated run is not faster')
    return old * 1000000 // new


def counts(root):
    root = Path(root)
    base = root / EVIDENCE
    for name in ('verify-evidence.py', 'verify-board-evidence.py'):
        subprocess.run([sys.executable, str(base / name)], cwd=root, check=True, stdout=subprocess.DEVNULL)
    sys.path.insert(0, str(root))
    sys.path.insert(0, str(root / 'tools'))
    try:
        from tools import gf16_ffn_vectors as vectors
    finally:
        sys.path.pop(0)
        sys.path.pop(0)

    # Simulation: five complete RTL replay captures against their saved oracles.
    replayed_values = 0
    for folder in sorted((base / 'simulation/replay').iterdir()):
        ref = json.loads(_read(folder, 'reference.json'))
        checked = vectors.validate(_read(folder, 'capture.txt'), ref, ref.get('run', 1))
        replayed_values += checked['stage_values'] + checked['actquant_values']
    if len(list((base / 'simulation/replay').iterdir())) != 5:
        raise ValueError('expected five replay captures')

    # Simulation clock comparison, result mode, serial vs accelerated.
    ref = json.loads(_read(base, 'reference-bos.json'))
    sim = {}
    for variant in ('baseline', 'accelerated'):
        sim[variant] = vectors.validate(_read(base / 'simulation' / variant / 'result', 'capture.txt'),
                                        {**ref, 'trace': 'result'}, ref['run'])
    sim_active = _ppm(sim['baseline']['clock_split']['total'], sim['accelerated']['clock_split']['total'])
    sim_loop = _ppm(sim['baseline']['projection_loop_clocks'], sim['accelerated']['projection_loop_clocks'])

    # Physical: every retained full and result-only capture, 3 runs x 2 modes.
    physical = {}
    for variant, name in (('baseline', 'bos'), ('accelerated', 'bos'), ('accelerated', 'zero')):
        folder = base / variant / name
        full_ref = json.loads(_read(folder, 'reference.json'))
        pair_ref = json.loads(_read(folder / 'result-only', 'reference.json'))
        physical[(variant, name, 'full')] = vectors.validate(_read(folder, 'capture.txt'), full_ref, full_ref['run'])
        physical[(variant, name, 'result')] = vectors.validate(
            _read(folder / 'result-only', 'capture.txt'), pair_ref, pair_ref['run'])
    phys = _ppm(physical[('baseline', 'bos', 'result')]['clock_split']['total'],
                physical[('accelerated', 'bos', 'result')]['clock_split']['total'])
    return (replayed_values, len(physical), sim_active, sim_loop, phys)


CLAIM = {
    'name': 'gf16_ffn_performance',
    'issues': [113],
    'spec': 'specs/memory/gf16_ffn_performance_evidence.t27',
    'vectors': 'conformance/memory_gf16_ffn_performance_evidence.json',
    'accept': 'tmgp_accept',
    'expected': [211200, 6, 1568006, 1649484, 1550876],
    'manifest': EVIDENCE + '/evidence-manifest.json',
    'scope': 'retained GF16 FFN simulation replays and physical AX7203 captures (2026-10-01); not a new board run',
    'bind': [
        EVIDENCE + '/**/*',
        'reports/numeric/gf16-ffn*.json',
        't27/rtl/gf16_scalar.t27', 't27/rtl/gf16_ffn.t27', 't27/rtl/gf16_wide_norm.t27', 't27/rtl/ffn_wide.t27',
        'rtl/t27/gf16_ffn.v', 'rtl/t27/gf16_wide_norm.v', 'tests/tb_gf16_ffn.v',
        'tools/gf16_ffn_vectors.py', 'tools/ffn_vectors.py', 'tools/gf16_wide_reference.py',
        'tools/gf16_reference.py', 'tools/ffn_reference.py', 'tools/matvec_device_model.py',
        'tools/uart_loader_protocol.py', 'tools/gf16_wide_build.py', 'tools/gf16_ffn_build.py',
        'tools/gf16_ffn_reference.py', 'tools/replay_gf16_ffn.py', 'tools/measure_gf16_ffn.py',
        'tools/bitnet_ffn_runtime.py', 'tools/capture_bitnet_layer0.py',
        'tools/evidence/gf16_ffn_performance.py', 'native/compiler.lock',
    ],
    'counts': counts,
}
