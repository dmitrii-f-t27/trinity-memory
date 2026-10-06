"""Claim ffn_clock_split: retained FFN clock-split board runs of issue #94 (PR #95)."""
import importlib.util

EVIDENCE = 'reports/fpga/ffn-clock-split-2026-09-30-757f191b'
STAGE5 = 'reports/fpga/ffn-q16-2026-09-29-eecc619f'


def counts(root):
    spec = importlib.util.spec_from_file_location('replay_ffn_clock_split', root / 'tools/replay_ffn_clock_split.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    r = module.replay()
    return (r['runs'], r['stage_values'], r['loads'], r['chunks'], r['compute_clocks'])


CLAIM = {
    'name': 'ffn_clock_split',
    'issues': [94],
    'scope': 'retained clock-split FFN board captures and loader receipts (seed27 and zero runs); not a new board run',
    'spec': 'specs/memory/ffn_clock_split_evidence.t27',
    'vectors': 'conformance/memory_ffn_clock_split_evidence.json',
    'accept': 'tmfc_accept',
    'expected': (2, 65536, 16, 13182, 118308783),
    'manifest': EVIDENCE + '/evidence-manifest.json',
    'bind': [
        EVIDENCE + '/**/*',
        STAGE5 + '/vectors-seed27/*', STAGE5 + '/vectors-zero/*',
        'tools/ffn_vectors.py', 'tools/ffn_reference.py', 'tools/matvec_device_model.py',
        'tools/uart_loader_protocol.py',
        'tools/replay_ffn_q16_board.py', 'tools/replay_ffn_clock_split.py', 'tools/evidence/ffn_clock_split.py',
    ],
    'counts': counts,
}
