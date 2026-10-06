"""Evidence claim: AX7203 matvec compute-pipeline speedup (#88), retained captures."""
from pathlib import Path
import importlib.util

_path = Path(__file__).with_name('_matvec_pipeline_replay.py')
_spec = importlib.util.spec_from_file_location('_matvec_pipeline_replay', _path)
_replay = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_replay)


def counts(root):
    r = _replay.replay(Path(root))
    return (r['captures'], r['rows'], r['bad_rows'],
            r['cycles']['baseline'], r['cycles']['pipeline'], r['ratio_ppm'])


CLAIM = {
    'name': 'matvec_pipeline',
    'issues': [88],
    'scope': 'retained AX7203 compute_pipeline 0/1 captures at d375d7ff (dense5 q_proj 320x2560, 60 MHz); not a new board run',
    'spec': 'specs/memory/matvec_pipeline_evidence.t27',
    'vectors': 'conformance/memory_matvec_pipeline_evidence.json',
    'accept': 'tmmp_accept',
    'expected': [24, 7680, 0, 235544, 143384, 1642749],
    'manifest': 'reports/fpga/matvec-pipeline-evidence-manifest.json',
    'bind': [
        'reports/fpga/matvec-compute-baseline-2026-09-28-d5.json',
        'reports/fpga/matvec-compute-baseline-2026-09-28-d5.uart/*',
        'reports/fpga/matvec-compute-pipeline-2026-09-28-d5.json',
        'reports/fpga/matvec-compute-pipeline-2026-09-28-d5.uart/*',
        'reports/fpga/matvec-pipeline-reference-y320.json',
        'reports/fpga/review-compute-pipeline-2026-09-29.md',
        'reports/ternary-check/matvec-2026-09-23.json',
        'tools/evidence/matvec_pipeline.py',
        'tools/evidence/_matvec_pipeline_replay.py',
        'tools/matvec_device_model.py',
        'tools/ddr3_read_model.py',
        'tools/uart_loader_protocol.py',
        'tools/fpga-matvec-run.py',
    ],
    'counts': counts,
}
