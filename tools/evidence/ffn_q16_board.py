"""Claim ffn_q16_board: retained Q16 FFN board runs of issue #92 (PR #93)."""
import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
EVIDENCE = 'reports/fpga/ffn-q16-2026-09-29-eecc619f'


def counts(root):
    spec = importlib.util.spec_from_file_location('replay_ffn_q16_board', root / 'tools/replay_ffn_q16_board.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    r = module.replay()
    # runs, stage values, loads, acknowledged and CRC-read-back chunks
    return (r['runs'], r['stage_values'], r['loads'], r['chunks'])


CLAIM = {
    'name': 'ffn_q16_board',
    'issues': [92],
    'scope': 'retained Q16 FFN board captures and loader receipts (seed27 and zero runs); not a new board run',
    'spec': 'specs/memory/ffn_q16_board_evidence.t27',
    'vectors': 'conformance/memory_ffn_q16_board_evidence.json',
    'accept': 'tmfq_accept',
    'expected': (2, 65536, 16, 13182),
    'manifest': EVIDENCE + '/evidence-manifest.json',
    'bind': [
        EVIDENCE + '/**/*',
        'tools/ffn_vectors.py', 'tools/ffn_reference.py', 'tools/matvec_device_model.py',
        'tools/uart_loader_protocol.py',
        'tools/replay_ffn_q16_board.py', 'tools/evidence/ffn_q16_board.py',
    ],
    'counts': counts,
}
