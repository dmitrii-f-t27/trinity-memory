#!/usr/bin/env python3
"""Offline replay of the retained FFN clock-split board runs of issue #94.

Same replay as tools/replay_ffn_q16_board.py (it is imported, not copied), applied to
reports/fpga/ffn-clock-split-2026-09-30-757f191b/ against the stage 5 vectors that
directory names (reports/fpga/ffn-q16-2026-09-29-eecc619f/vectors-*). Additionally the
`k` lines of each capture are parsed by ffn_vectors.validate(): the two wait counters must
be present, fit in the total, and the compute remainder is recomputed and compared with the
recorded split. Never opens hardware or the network.
"""
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
import replay_ffn_q16_board as base  # noqa: E402

EVIDENCE = ROOT / 'reports/fpga/ffn-clock-split-2026-09-30-757f191b'


def replay():
    totals, compute, split_runs = [0, 0, 0, 0], set(), 0
    gates = json.loads((EVIDENCE / 'build-gates.json').read_text())
    boot = json.loads((EVIDENCE / 'boot/boot.json').read_text())
    base.need(gates['bitstream_sha256'] == boot['bitstream_sha256'] and all(boot['checks'].values()), 'boot or gate identity')
    base.need(gates['source']['head'] == '757f191b9c0b41b5c5bf92b2bd0660e567aa8a18' and not gates['source']['dirty'], 'source of record')
    for run, vectors, run_id in base.RUNS:
        values, loads, chunks, _, split = base.replay_run(run, vectors, run_id, EVIDENCE)
        base.need(split is not None, f'{run}: no clock split in the capture')
        base.need(split['compute'] + split['report_wait'] + split['memory_wait'] == split['total'], f'{run}: split does not add up')
        compute.add(split['compute'])
        split_runs += 1
        totals[0] += 1; totals[1] += values; totals[2] += loads; totals[3] += chunks
    base.need(len(compute) == 1, 'compute clocks differ between the two runs')
    return {'runs': totals[0], 'stage_values': totals[1], 'loads': totals[2], 'chunks': totals[3],
            'split_runs': split_runs, 'compute_clocks': compute.pop()}


if __name__ == '__main__':
    print(json.dumps(replay()))
