"""Evidence claim ddr3_captures: retained DDR3 board captures (#61 bring-up, #62 read path).

counts() replays the committed UART transcripts: it checks their sha256, decodes them
again with tools/fpga-ddr3-capture.py, requires the result to equal the kept record, and
counts what the decoded results say. It also runs the repository's own unit tests for the
committed captures. Nothing here touches hardware or the network.
"""
import hashlib
import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

TESTS = [
    'tests.test_ddr3_bringup.CommittedEvidence.test_bringup_captures',
    'tests.test_ddr3_bringup.CommittedEvidence.test_pattern_totals_behind_the_docs',
    'tests.test_ddr3_reader.ReaderBoardRecords.test_records_decode_again',
    'tests.test_ddr3_reader.ReaderBoardRecords.test_pinned_board_results',
    'tests.test_ddr3_reader.ReaderBoardRecords.test_pinned_board_results_of_the_build_of_record',
]
BIST_READS = (1 << 25) - 1


def _capture_module(root):
    sys.path.insert(0, str(root / 'tools'))
    spec = importlib.util.spec_from_file_location('ddr3_captures_decoder', root / 'tools/fpga-ddr3-capture.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _entries(capture, directory, record):
    tsv = capture.read_kept(directory / record['uart_transcript']['file'])
    raw = capture.read_kept(directory / record['uart_transcript']['raw_file'])
    if hashlib.sha256(tsv).hexdigest() != record['uart_transcript']['sha256']:
        raise ValueError('transcript hash differs: ' + str(directory))
    if hashlib.sha256(raw).hexdigest() != record['uart_transcript']['raw_sha256']:
        raise ValueError('raw transcript hash differs: ' + str(directory))
    entries = []
    for line in tsv.decode().splitlines():
        if line.startswith('#') or not line.strip():
            continue
        t_s, _utc, text = line.split('\t', 2)
        entries.append({'t_s': float(t_s), 'line': text})
    return entries, raw


def _unit_tests(root):
    run = subprocess.run([sys.executable, '-m', 'unittest', *TESTS], cwd=root, capture_output=True, text=True)
    if run.returncode != 0:
        raise ValueError('replay unit tests failed:\n' + run.stderr[-2000:])
    ran = re.search(r'Ran (\d+) tests?', run.stderr)
    if not ran or int(ran.group(1)) != len(TESTS) or 'skipped' in run.stderr:
        raise ValueError('replay unit tests did not all run: ' + run.stderr[-500:])


def counts(root):
    root = Path(root)
    try:
        import numpy  # noqa: F401  the host model check of the reader runs needs it
    except ImportError:
        raise ValueError('numpy is required: the reader replay must check runs against the host model')
    _unit_tests(root)
    capture = _capture_module(root)

    paths = sorted(root.glob('reports/fpga/ddr3-bringup-*/*/capture.json*'))
    captures = clean_x16 = clean_x32 = pattern_errors = pattern_passes = 0
    for path in paths:
        record = json.loads(capture.read_kept(path.with_name('capture.json')))
        entries, raw = _entries(capture, path.parent, record)
        expect = record['expect']
        if record['board']['dna'] != '0x00389c0c2d85e85c':
            raise ValueError('wrong board: ' + str(path))
        if expect.get('uart_debug_bist'):
            again = capture.decode_bist_debug(raw, entries, load_end_s=record['run'].get('load_end_s'))
            if again != record['decoded_bist']:
                raise ValueError('BIST record differs on replay: ' + str(path))
            clean = again['pass'] and again['correct_read_data'] == BIST_READS
            errors = passes = 0
        else:
            again = capture.decode(entries, expect_build_id=expect['build_id'], expect_lanes=expect['lanes'],
                                   expect_period_ps=expect['period_ps'], load_end_s=record['run'].get('load_end_s'),
                                   nominal_hz=expect['nominal_hz'], expect_pattern=expect.get('pattern'))
            if json.loads(json.dumps(again)) != record['decoded']:
                raise ValueError('record differs on replay: ' + str(path))
            checks = again['checks']
            clean = bool(again['pass'] and checks['calib_complete'] and checks['done_calibrate']
                         and checks['no_return_to_idle'])
            pattern = again.get('pattern')
            errors = (pattern['totals']['bit_errors'] + pattern['totals']['bad_bursts']) if pattern else 0
            passes = pattern['totals']['passes'] if pattern else 0
            if clean and pattern and (pattern['totals']['clean_passes'] != passes or passes == 0):
                clean = False
        captures += 1
        if clean:
            lanes = 2 if expect.get('uart_debug_bist') else expect['lanes']
            if lanes == 2:
                clean_x16 += 1
            elif lanes == 4:
                clean_x32 += 1
            pattern_errors += errors
            pattern_passes += passes
    if pattern_passes == 0:
        raise ValueError('no pattern pass replayed')

    loads = sorted(root.glob('reports/fpga/ddr3-reader-*/load*/'))
    reader_runs = reader_defects = loads_with_runs = 0
    for directory in loads:
        record = json.loads(capture.read_kept(directory / 'capture.json'))
        entries, _raw = _entries(capture, directory, record)
        expect = record['expect']
        again = capture.decode(entries, expect_build_id=expect['build_id'], expect_lanes=expect['lanes'],
                               expect_period_ps=expect['period_ps'], load_end_s=record['run'].get('load_end_s'),
                               nominal_hz=expect['nominal_hz'], expect_pattern=expect.get('pattern'),
                               expect_reader=True, reader_model_runs=3)
        kept = record['decoded']
        for key in ('final', 'pass', 'checks'):
            if again[key] != kept[key]:
                raise ValueError(f'reader {key} differs on replay: {directory}')
        runs = again['reader']['runs']
        if len(runs) != len(kept['reader']['runs']):
            raise ValueError('reader runs differ on replay: ' + str(directory))
        if runs:
            loads_with_runs += 1
            if not again['pass']:
                raise ValueError('reader load did not pass: ' + str(directory))
            if not all(r['checks']['equals_host_model'] for r in runs[:3]):
                raise ValueError('reader run differs from the host model: ' + str(directory))
        for r in runs:
            reader_defects += r['bad_words'] + r['invalid_groups'] + r['consumer_stalls'] + r.get('stray_acks', 0)
        reader_runs += len(runs)
    if loads_with_runs == 0 or reader_runs == 0:
        raise ValueError('no reader run replayed')
    return (captures, clean_x16, clean_x32, pattern_errors, reader_runs, reader_defects)


CLAIM = {
    'name': 'ddr3_captures',
    'issues': [61, 62],
    'scope': ('retained DDR3 board captures of 2026-09-24 re-decoded from their committed UART transcripts '
              '(not a new board run); the Icarus simulation, seed sweeps, probes and raw loader files are not replayed'),
    'spec': 'specs/memory/ddr3_captures_evidence.t27',
    'vectors': 'conformance/memory_ddr3_captures_evidence.json',
    'accept': 'tmdc_accept',
    'expected': [33, 10, 17, 0, 3202, 0],
    'manifest': 'reports/fpga/ddr3-captures-evidence-manifest.json',
    'bind': [
        'reports/fpga/ddr3-bringup-*/*/*',
        'reports/fpga/ddr3-reader-2026-09-24-*/load*/*',
        'reports/fpga/ddr3-reader-summary-*.json',
        'specs/memory/ddr3_captures_evidence.t27',
        'tools/evidence/ddr3_captures.py',
        'tools/fpga-ddr3-capture.py',
        'tools/ddr3_read_model.py',
        'tools/ddr3_pattern_model.py',
        'tools/bram_trit_model.py',
        'tools/uberddr3-bist-model.py',
        'tests/test_ddr3_bringup.py',
        'tests/test_ddr3_reader.py',
    ],
    'counts': counts,
}
