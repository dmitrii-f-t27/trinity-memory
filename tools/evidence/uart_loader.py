"""Claim uart_loader: retained UART loader board runs of issue #63 (PRs #72, #74)."""
import importlib.util

BRAM = 'reports/fpga/uart-loader-2026-09-24-1d474000'
DDR3 = 'reports/fpga/uart-loader-ddr3-2026-09-24-a9a56541-seed3'


def counts(root):
    spec = importlib.util.spec_from_file_location('replay_uart_loader', root / 'tools/replay_uart_loader.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    r = module.replay()
    return (r['runs'], r['load_acks'], r['readback_bytes'], r['nak_lines'], r['ddr3_calibrated_runs'])


CLAIM = {
    'name': 'uart_loader',
    'issues': [63],
    'scope': 'retained raw UART streams of ten loader board runs (block RAM build 1d474000, DDR3 build a9a56541); not a new board run',
    'spec': 'specs/memory/uart_loader_evidence.t27',
    'vectors': 'conformance/memory_uart_loader_evidence.json',
    'accept': 'tmul_accept',
    'expected': (10, 339, 1360040, 10, 5),
    'manifest': DDR3 + '/evidence-manifest.json',
    'bind': [
        # only the runs and build records the replay reads; superseded and failed builds are not bound
        *[f'{BRAM}/{n}.json' for n in ('run1-clean', 'run2-faults', 'run3-clean', 'run4-drain', 'run5-baud')],
        *[f'{BRAM}/{n}.json.rx.bin.gz' for n in ('run1-clean', 'run2-faults', 'run3-clean', 'run4-drain', 'run5-baud')],
        *[f'{DDR3}/{n}.json' for n in ('run1-rows-dense5', 'run2-rows-baseline2', 'run4-faults',
                                       'run5-recheck-rows-dense5', 'run6-recheck-rows-baseline2')],
        *[f'{DDR3}/{n}.json.rx.bin.gz' for n in ('run1-rows-dense5', 'run2-rows-baseline2', 'run4-faults',
                                                  'run5-recheck-rows-dense5', 'run6-recheck-rows-baseline2')],
        'tools/uart_loader_protocol.py', 'tools/replay_uart_loader.py', 'tools/evidence/uart_loader.py',
    ],
    'counts': counts,
}
