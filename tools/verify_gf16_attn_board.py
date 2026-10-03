"""Recheck attention UART captures and reconstruct every loaded byte from CRC frames.

Supports a full run and its optional result-only replay on unchanged DDR inputs.
Run from the repository root. Raw readback streams and payloads remain local;
the JSON output records hashes, coverage and any rejected/retried CRC frames.
"""
import argparse
import gzip
import hashlib
import json
from pathlib import Path
import re
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
sys.path.insert(0,str(ROOT/'tools'))
from tools import gf16_attn_vectors as vectors
import ffn_vectors
import uart_loader_protocol as proto


def sha(data):
    return hashlib.sha256(data).hexdigest()


def reconstruct_payload(received,expected,address):
    """Coverage comes only from CRC-valid frames; retries may fill rejected spans."""
    decoder=proto.StreamDecoder()
    got=bytearray(len(expected));seen=bytearray(len(expected));count=0;rejected=[]
    for off in range(0,len(received),65536):
        for event in decoder.feed(received[off:off+65536]):
            if event.kind!='readback' or not address<=event.addr<address+len(expected):continue
            if not event.crc_ok:
                rejected.append(event.addr);continue
            start=event.addr-address;end=start+len(event.data)
            if end>len(expected) or event.data!=expected[start:end]:
                raise ValueError('CRC-valid readback differs from payload')
            got[start:end]=event.data;seen[start:end]=b'\1'*len(event.data);count+=1
    if not expected or not all(seen) or bytes(got)!=expected:
        raise ValueError('incomplete CRC-valid payload coverage')
    return dict(bytes=len(expected),crc_valid_blocks=count,payload_sha256=sha(expected),
                raw_sha256=sha(received),crc_rejected_frames=rejected)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--run', type=Path, required=True)
    ap.add_argument('--vectors', type=Path, required=True)
    ap.add_argument('--boot', type=Path, required=True)
    ap.add_argument('--output', type=Path, required=True)
    args = ap.parse_args()
    assert not args.output.exists()
    folder = args.run
    ref = json.loads((args.vectors / 'reference.json').read_text())
    manifest = json.loads((args.vectors / 'inputs.json').read_text())
    run = ref.get('run', 1)
    positions=ref['positions']
    vectors.validate_board_inputs(args.vectors, manifest, ref['stages'], run, positions,
                                  result_only=ref.get('trace')=='result')
    boot = json.loads(args.boot.read_text())
    result = json.loads((folder / 'result.json').read_text())
    command = json.loads((folder / 'command.json').read_text())
    assert all(boot['checks'].values()) and boot['checks']['gf16_attn_header']
    assert boot['run']['dna'] == '0x00389c0c2d85e85c'
    assert int(boot['run']['idcode'], 16) == 0x3636093
    assert result['bitstream_sha256'] == boot['bitstream_sha256'] == command['bitstream_sha256']
    raw = (folder / 'capture.txt').read_bytes()
    checked = vectors.validate(raw, ref['stages'], run, positions, ref.get('trace')=='result')
    assert ffn_vectors.doorbell_acknowledged(raw)
    for key, value in checked.items():
        assert result[key] == value, key

    # Independently reconstruct flat stage arrays from the per-position wire tags.
    # Index/order/completion checks above remain strict; this independently checks
    # the value stream against the oracle stages (including all causal score rows).
    lines = [(t.decode(), int(i, 16), int(v, 16)) for t, i, v in
             re.findall(rb'([A-Za-z])([0-9a-fA-F]{8})([0-9a-fA-F]{10})\n', raw)]
    for tag, stage in [('g','n'),('h','q'),('i','k'),('j','v'),('k','qr'),('l','kr'),
                       ('m','sc'),('n','a'),('o','c'),('p','o'),('q','r')]:
        if ref.get('trace')=='result' and stage!='r':continue
        assert [v for t,i,v in lines if t==tag] == [v & 0xffffffff for v in ref['stages'][stage]], stage

    paired = None
    pairdir = folder/'result-only'
    if pairdir.exists():
        assert ref.get('trace', 'full') == 'full'
        pairref = json.loads((pairdir/'reference.json').read_text())
        assert pairref == {**ref, 'trace':'result', 'run':run+1}
        descriptor = ((run+1) | (vectors.MAGIC << 32) | (positions << 64) | (1 << 96)).to_bytes(16, 'little')
        assert (pairdir/'doorbell.bin').read_bytes() == descriptor
        pairraw = (pairdir/'capture.txt').read_bytes()
        paired = vectors.validate(pairraw, pairref['stages'], run+1, positions, True)
        assert ffn_vectors.doorbell_acknowledged(pairraw)
        pairresult = json.loads((pairdir/'result.json').read_text())
        assert all(pairresult[k] == v for k,v in paired.items())
        assert pairresult['bitstream_sha256'] == result['bitstream_sha256']
        assert pairresult['capture_baud'] == result['capture_baud']
        assert pairresult['input_reuse'] == {
            'source':'../', 'first_run':run,
            'manifest_sha256':sha((args.vectors/'inputs.json').read_bytes()),
            'doorbell_sha256':sha(descriptor),
            'scope':'same freshly uploaded/readback-verified inputs; only descriptor changed'}
        pair_y = [(int(i,16),int(v,16)) for i,v in re.findall(rb'q([0-9a-fA-F]{8})([0-9a-fA-F]{10})\n',pairraw)]
        assert pair_y == [(i % 2560, v & 0xffffffff) for i,v in enumerate(ref['stages']['r'])]
        paired['input_reuse'] = pairresult['input_reuse']

    readbacks = {}
    for name in ('qualification', 'scales', 'w_in', 'w_sub', 'x', 'rope', 'q', 'k', 'v', 'o'):
        label = name if name == 'qualification' else 'load-' + name
        receipt = json.loads((folder / (label + '.json')).read_text())
        assert receipt['pass'] and all(receipt['checks'].values()) and receipt['baud'] == 921600
        expected = (bytes(range(256)) * 128 if name == 'qualification' else
                    (args.vectors / manifest[name]['file']).read_bytes())
        address = 0x2000000 if name == 'qualification' else manifest[name]['byte_address']
        assert receipt['addr'] == address and receipt['transfer']['payload_bytes'] == len(expected)
        rr = receipt['rx_raw']
        received = gzip.decompress((folder / rr['file']).read_bytes())
        assert len(received) == rr['bytes'] and sha(received) == rr['sha256_uncompressed']
        readbacks[name] = reconstruct_payload(received,expected,address)
        readbacks[name]['retransmits'] = receipt['transfer']['retransmits']
        print(name, 'full raw readback PASS', len(expected), flush=True)

    rates=[('uart-fast',65),('uart-restored',521)]
    if (folder/'uart-capture.json').exists():
        rates.insert(1,('uart-capture',round(60000000/result['capture_baud'])))
    for label, divisor in rates:
        r = json.loads((folder / (label + '.json')).read_text())
        assert r['switch']['switched'] and r['after']['baud_div'] == divisor
        assert sha((folder / (label + '.rx.bin')).read_bytes()) == r['rx_sha256']
    thermal = [json.loads(line) for line in (folder / 'thermal.jsonl').read_text().splitlines()]
    assert thermal and all(t['returncode'] == 0 and 0 < t['temperature_c'] < 70 for t in thermal)
    peaks=[float(re.search(r'"maxtemp"\s*:\s*([0-9.]+)',t['output']).group(1)) for t in thermal]
    assert all(p<70 for p in peaks)
    checked.update(bitstream_sha256=boot['bitstream_sha256'], readbacks=readbacks,
        input_readback_bytes=sum(r['bytes'] for name,r in readbacks.items() if name!='qualification'),
        capture_baud=result['capture_baud'],hardware_peak_temperature_c=max(peaks),
        temperatures_c=dict(min=min(t['temperature_c'] for t in thermal),
                            max=max(t['temperature_c'] for t in thermal), samples=len(thermal)),
        uart_restored=115200, tool_commit=command['tool_commit'],
        verifier_sha256=sha(Path(__file__).read_bytes()),
        reference_sha256=sha((args.vectors / 'reference.json').read_bytes()),
        manifest_sha256=sha((args.vectors / 'inputs.json').read_bytes()))
    if paired is not None:
        checked['paired_result_only'] = paired
    args.output.write_text(json.dumps(checked, indent=2) + '\n')
    print('PASS', args.output, flush=True)


if __name__=='__main__':
    main()
