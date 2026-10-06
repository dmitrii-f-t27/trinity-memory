#!/usr/bin/env python3
"""Offline replay of the retained UART loader board runs of issue #63.

Reads only committed files: the loader records (trinity.uart-loader-capture.v2)
and the raw byte streams the host received from the board (*.rx.bin.gz). Never
opens a serial port, a JTAG cable or the network.

Recomputed from the raw received bytes, for ten runs (five on the block-RAM build
1d474000, five on the DDR3 build a9a56541 seed 3):
  * the stream's sha256 and length equal the record's;
  * proto.StreamDecoder re-decodes every line and read-back frame; every
    read-back frame of the payload range is CRC-checked, the frames are
    reassembled in the record's order, and the bytes' sha256 equals the payload
    sha256 of the record, and its TMEM header equals the record's (the container is
    not decoded: that needs the native t27 library);
  * every load chunk has an ack line (ok or duplicate) with its seq, command, length;
  * the nak lines by reason equal the record's retransmits (crc, timeout);
  * the build id of the status lines equals the build named in the directory;
  * DDR3 runs: the last status dump in the stream has calibration complete, state 23,
    highest 23, no return to IDLE and no clock lost since calibration.
Not recomputed: the payload file itself (qproj rows, not retained: only its sha256
is), timing and rates (host time stamps), the 1.3 MB q_proj runs and run 6 of the
block-RAM build (their raw streams are not committed, only the sha256 in the record).
"""
import gzip
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tools'))
import uart_loader_protocol as proto  # noqa: E402

BRAM = 'reports/fpga/uart-loader-2026-09-24-1d474000'
DDR3 = 'reports/fpga/uart-loader-ddr3-2026-09-24-a9a56541-seed3'
RUNS = tuple((BRAM, n, 0x1d474000, False) for n in (
    'run1-clean', 'run2-faults', 'run3-clean', 'run4-drain', 'run5-baud')) + \
    tuple((DDR3, n, 0xa9a56541, True) for n in (
        'run1-rows-dense5', 'run2-rows-baseline2', 'run4-faults',
        'run5-recheck-rows-dense5', 'run6-recheck-rows-baseline2'))


def need(condition, message):
    if not condition:
        raise ValueError(message)


def last_status(events):
    """The last complete status dump (every index 0..count-1 of one seq) in the stream."""
    groups, best = {}, None
    for e in events:
        if e.kind == 'line' and e.tag == 'C' and e.check_ok:
            key = (e.a >> 24, (e.a >> 16) & 0xFF)
            groups.setdefault(key, {})[e.a & 0xFFFF] = e
            if len(groups[key]) == key[1]:
                best = list(groups[key].values())
                groups[key] = {}
    return proto.decode_status(best) if best else None


def replay_run(directory, name, build, ddr3):
    record = json.loads((ROOT / directory / (name + '.json')).read_text())
    raw = gzip.open(ROOT / directory / (name + '.json.rx.bin.gz')).read()
    rx = record['rx_raw']
    need(record['pass'] is True and all(record['checks'].values()), name + ': record does not pass')
    need(hashlib.sha256(raw).hexdigest() == rx['sha256_uncompressed'] and len(raw) == rx['bytes'], name + ': raw stream differs from the record')
    events = proto.StreamDecoder().feed(raw)
    payload, transfer = record['payload'], record['transfer']
    need(transfer['identical'] is True and transfer['chunks'] == transfer['acked'], name + ': transfer not identical/acked')

    # read-back: reassemble the payload range from CRC-checked frames in the record's order
    cursor, data = 0, bytearray()
    for read in transfer['reads']:
        seq = read['attempts'][-1]['seq']
        for i in range(cursor, len(events)):
            e = events[i]
            if e.kind == 'readback' and e.seq == seq and e.addr == read['addr'] and len(e.data) == read['len']:
                need(e.crc_ok, f'{name}: read-back frame at {read["addr"]:#x} fails its CRC')
                data += e.data
                cursor = i + 1
                break
        else:
            raise ValueError(f'{name}: read-back frame at {read["addr"]:#x} missing from the raw stream')
    need(len(data) == payload['bytes'] and hashlib.sha256(data).hexdigest() == payload['sha256'],
         name + ': reassembled read-back differs from the payload hash')
    need(bytes(data[:24]).hex() == payload['tmem_header_hex'], name + ': TMEM header of the read-back differs from the record')

    # load acks, in order
    cursor, acks = 0, 0
    for chunk in transfer['per_chunk']:
        for i in range(cursor, len(events)):
            e = events[i]
            if e.kind == 'line' and e.tag == 'A' and e.check_ok:
                w = e.resp()
                if w['seq_known'] and w['seq'] == chunk['seq'] and w['cmd'] == proto.CMD_LOAD and w['len'] == chunk['len'] \
                        and w['reason'] in (proto.R_OK, proto.R_DUP):
                    acks += 1
                    cursor = i + 1
                    break
        else:
            raise ValueError(f'{name}: no ack for chunk {chunk["index"]} in the raw stream')

    # naks: every retransmission after a refusal is a nak line in the stream
    naks = {}
    for e in events:
        if e.kind == 'line' and e.tag == 'N' and e.check_ok:
            reason = e.resp()['reason']
            if reason in (proto.R_CRC, proto.R_TIMEOUT):
                naks[reason] = naks.get(reason, 0) + 1
    want = transfer['retransmits']
    need(naks.get(proto.R_CRC, 0) == want.get('crc', 0) and naks.get(proto.R_TIMEOUT, 0) == want.get('timeout', 0),
         f'{name}: nak lines differ from the record {naks} vs {want}')

    status = last_status(events)
    need(status is not None and status['build_id'] == build, name + ': build id of the status lines')
    calib = 0
    if ddr3:
        word = proto.decode_calib(status['calib'])
        need((word['calib_complete'], word['state'], word['highest'], word['returns_to_idle']) == (1, 23, 23, 0)
             and status['calib_lost_clocks'] == 0, name + ': calibration not held in the last status')
        calib = 1
    return acks, len(data), sum(naks.values()), calib


def replay():
    totals = [0, 0, 0, 0, 0]
    for directory, name, build, ddr3 in RUNS:
        acks, size, naks, calib = replay_run(directory, name, build, ddr3)
        totals = [totals[0] + 1, totals[1] + acks, totals[2] + size, totals[3] + naks, totals[4] + calib]
    return dict(zip(('runs', 'load_acks', 'readback_bytes', 'nak_lines', 'ddr3_calibrated_runs'), totals))


if __name__ == '__main__':
    print(json.dumps(replay()))
