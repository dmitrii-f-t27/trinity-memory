"""Evidence claim stage2_ddr3: issues #65 and #67 (retained measurements, not a new board run).

counts(root) runs the existing offline checker tools/stage2-report.py (its build()
and check()) and the matvec_device_model decoder it uses, then recomputes from the
retained captures:

  rate_runs                     runs (12 per format, enforced: dense5 and baseline2,
                                2026-09-27 #65 captures) whose words*lanes is the
                                819,200-weight chunk and whose recorded weights/s
                                equals weights * 60 MHz / cycles (to 0.05)
  bad_words                     sum of bad_words over those runs and the two golden
                                run summaries (decode_run_summary line n)
  ratio_e4                      round(1e4 * mean(dense5 rate) / mean(baseline2 rate)),
                                from the recomputed rates
  golden_first_rows             first device Y rows equal to the reference after
                                40-bit sign wrap (4 per format; baseline2 against the dense5 reference rows, same chunk)
  reader_model_checked_runs     direct-reader runs with equals_host_model, counted
                                from the archived capture.json.gz files

Also enforced (raise, not a count): stage2-report.py --check passes all three
checks (JSON, HTML, docs/hardware.md rebuilt from the captures), and the direct
reader's recounted bad_words is 0 (folded into bad_words).

Not recomputable here: the 320/320 bit-exact flag of the golden captures (the
retained files hold the flag and four rows, not the 320 Y lines), the board runs.
"""
import contextlib
import gzip
import importlib.util
import io
import json
import statistics
import sys
from pathlib import Path

TRITS = 819200
CTRL_HZ = 60_000_000


def _tool(root):
    path = root / 'tools' / 'stage2-report.py'
    spec = importlib.util.spec_from_file_location('stage2_report_for_claim', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _wrap40(value):
    value &= (1 << 40) - 1
    return value - (1 << 40) if value >> 39 else value


def counts(root):
    tool = _tool(Path(root))
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        ok = tool.check(tool.build())
    report_checks = out.getvalue().count(': PASS')
    if not ok or report_checks != 3:
        raise ValueError('stage2-report.py --check failed: ' + out.getvalue())

    runs_n, rates, rate_runs, bad = {}, {}, 0, 0
    for key, fmt, lanes in (('measure_d5', 'dense5', 80), ('measure_b2', 'baseline2', 64)):
        measurement = tool.load(key)
        runs = tool.corrected_runs(measurement)
        runs_n[fmt] = len(runs)
        recomputed = []
        for r in runs:
            if r['words'] * lanes != TRITS or r['cycles'] <= 0:
                raise ValueError(fmt + ': run does not cover the 819,200-weight chunk')
            rate = TRITS * CTRL_HZ / r['cycles']
            if abs(rate - r['weights_per_s']) <= 0.05 + 1e-9:
                rate_runs += 1
            recomputed.append(rate)
            bad += r['bad_words']
        rates[fmt] = statistics.mean(recomputed)
    if runs_n != {'dense5': 12, 'baseline2': 12}:
        raise ValueError('expected 12 runs per format: %r' % runs_n)
    ratio_e4 = round(1e4 * rates['dense5'] / rates['baseline2'])

    first_rows = 0
    d5 = tool.load('golden_d5')
    reference = [int(v) for v in d5['first_expected']]  # same chunk and activations in both formats
    for key, field in (('golden_d5', 'summary_lines'), ('golden_b2', 'summary')):
        golden = tool.load(key)
        decoded = tool.decode_run_summary(golden[field])
        bad += decoded['bad_words']
        # dense5 stores (row, 40-bit device word) pairs; baseline2 stores decoded values.
        got = ([_wrap40(v) for _, v in golden['first_y']] if key == 'golden_d5'
               else [int(v) for v in golden['first_y']])
        if got != reference or len(got) != 4:
            raise ValueError(key + ': first rows differ from the reference')
        first_rows += len(got)

    reader_checked = reader_bad = 0
    for load in tool.load('reader_summary')['board']['seed6']:
        capture = json.loads(gzip.decompress((Path(root) / load['capture'] / 'capture.json.gz').read_bytes()))
        for r in capture['decoded']['reader']['runs']:
            reader_checked += bool(r['checks']['equals_host_model'])
            reader_bad += r['bad_words']
    return (rate_runs, bad + reader_bad, ratio_e4, first_rows, reader_checked)


CLAIM = {
    'name': 'stage2_ddr3',
    'issues': [65, 67],
    'scope': 'retained 2026-09-27 DDR3 matvec measurements and the stage-2 report rebuilt from them; '
             'not a new board run; DDR3 bandwidth as the bottleneck is not established',
    'spec': 'specs/memory/stage2_ddr3_evidence.t27',
    'vectors': 'conformance/memory_stage2_ddr3_evidence.json',
    'accept': 'tmsd_accept',
    'manifest': 'reports/stage2/evidence-manifest.json',
    'expected': [24, 0, 10000, 8, 1653],
    'bind': [
        'specs/memory/stage2_ddr3_evidence.t27',
        'tools/evidence/stage2_ddr3.py',
        'tools/stage2-report.py',
        'tools/matvec_device_model.py',
        'tools/ddr3_read_model.py',
        'docs/hardware.md',
        'reports/stage2/stage2.json',
        'reports/stage2/index.html',
        'reports/fpga/ddr3-matvec-measure-2026-09-27-*.json',
        'reports/fpga/matvec-capture-2026-09-27-m6d5-golden*.json',
        'reports/fpga/ddr3-build-2026-09-27-twoclock-m6d5*/build.json',
        'reports/fpga/ddr3-reader-summary-2026-09-24-a6d9745f-x16.json',
        'reports/fpga/ddr3-build-2026-09-24-a6d9745f-x16-reader-seed6/build.json',
        'reports/fpga/ddr3-reader-2026-09-24-a6d9745f-x16-seed6/*/capture.json.gz',
    ],
    'counts': counts,
}
