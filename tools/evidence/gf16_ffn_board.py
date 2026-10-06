"""Evidence claim gf16_ffn_board: retained AX7203 captures of the full GF16 FFN (issue #111).

Replays the committed UART captures of two physical runs (real BOS input and
zero input) of the gf16-ffn-v1 layer-0 FFN. Nothing here touches hardware.

What the counts are:
  runs            captured board runs whose capture reproduces, line by line, the
                  retained integer reference (tools/gf16_ffn_vectors.validate,
                  the function verify-captures.py itself uses): bos and zero
  stage_values    stage values compared exactly per run (2560+6912*4+2560 = 32768)
  actquant_values ActQuant values (and signed codes) compared exactly per run
  routed_60mhz    routed clock domains in build.json whose verdict is PASS at a
                  60 MHz target (clk_ctrl, 62.64 MHz)
The run must also pass the retained verify-captures.py unchanged (it additionally
checks boot checks, the bitstream hash shared by build and boot, doorbell
acknowledgement, hash chains of reference/inputs, a 70 C thermal limit, and that
the BOS reference matches reports/numeric/gf16-ffn.json).
"""
import gzip
import json
import os
from pathlib import Path
import subprocess
import sys

EVIDENCE = 'reports/fpga/gf16-ffn-2026-10-01-24917fa7'
RUNS = ('bos', 'zero')


def _read(folder, name):
    path = folder / name
    return path.read_bytes() if path.exists() else gzip.decompress((folder / (name + '.gz')).read_bytes())


def counts(root):
    root = Path(root)
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1')
    subprocess.run([sys.executable, str(root / EVIDENCE / 'verify-captures.py')], check=True, env=env,
                   cwd=root, stdout=subprocess.DEVNULL)
    sys.path.insert(0, str(root))
    sys.path.insert(0, str(root / 'tools'))
    sys.dont_write_bytecode = True
    try:
        from tools import gf16_ffn_vectors as vectors
    finally:
        sys.path.pop(0)
        sys.path.pop(0)
    base = root / EVIDENCE
    seen = []
    for name in RUNS:
        folder = base / name
        ref = json.loads(_read(folder, 'reference.json'))
        checked = vectors.validate(_read(folder, 'capture.txt'), ref, ref['run'])
        assert checked['pass'] and checked['profile'] == 'gf16-ffn-v1' and checked.get('trace') != 'result'
        seen.append((checked['stage_values'], checked['actquant_values']))
    if len(set(seen)) != 1:
        raise ValueError('board runs disagree on the number of compared values: %r' % seen)
    build = json.loads(_read(base / 'build', 'build.json'))
    routed = sum(1 for c in build['nextpnr']['clocks_routed'] if c['verdict'] == 'PASS' and c['target_mhz'] == 60)
    if routed != len(build['nextpnr']['clocks_routed']):
        raise ValueError('a routed clock domain missed 60 MHz')
    return (len(seen), seen[0][0], seen[0][1], routed)


CLAIM = {
    'name': 'gf16_ffn_board',
    'issues': [111],
    'scope': ('retained UART captures of two physical AX7203 runs of the gf16-ffn-v1 layer-0 FFN '
              '(2560->6912->2560) replayed offline against the retained integer reference; '
              'not a new board run'),
    'spec': 'specs/memory/gf16_ffn_board_evidence.t27',
    'vectors': 'conformance/memory_gf16_ffn_board_evidence.json',
    'accept': 'tmgb_accept',
    'expected': [2, 32768, 9472, 1],
    'manifest': EVIDENCE + '/evidence-manifest.json',
    'bind': [
        EVIDENCE + '/README.md', EVIDENCE + '/*.py',
        EVIDENCE + '/boot/*', EVIDENCE + '/bos/*', EVIDENCE + '/zero/*', EVIDENCE + '/build/*',
        EVIDENCE + '/diagnostics/*/*',
        'reports/numeric/gf16-ffn.json',
        'tools/gf16_ffn_vectors.py', 'tools/gf16_wide_reference.py', 'tools/ffn_vectors.py',
        'tools/ffn_reference.py', 'tools/matvec_device_model.py', 'tools/uart_loader_protocol.py',
        'tools/evidence/gf16_ffn_board.py',
    ],
    'counts': counts,
}
