"""Issue #115: load, read back, run and capture the layer-0 attention on a
pre-booted board.

Requires a passing hash-checked --gf16-attn boot report. Checks XADC every
ten seconds during transfers and capture. No bitstream loading or flash
write occurs here.
"""
import argparse
import datetime
import hashlib
import importlib.util
import json
from pathlib import Path
import random
import re
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tools'))
import uart_loader_protocol as proto  # noqa: E402
import ffn_vectors as fv  # noqa: E402
from tools import gf16_attn_vectors as av  # noqa: E402
from uart_session import restoring_baud  # noqa: E402


def capture_stream(port, payload, raw, run_id, temperature, timeout=180):
    """Drain UART continuously, including while the main thread checks XADC."""
    stop = threading.Event(); done = threading.Event(); errors = []
    finish = re.compile(rb'z' + f'{run_id:08x}'.encode() + rb'0000000000\n')
    fatal = re.compile(rb'E[0-9a-fA-F]{18}\n')

    def receive():
        tail = b''
        try:
            while not stop.is_set():
                chunk = port.read(4096)
                if not chunk:
                    continue
                raw.extend(chunk)
                window = tail + chunk; tail = window[-20:]
                if fatal.search(window):
                    errors.append(RuntimeError('attention fatal error in capture')); done.set(); return
                if finish.search(window): done.set(); return
        except Exception as error:
            errors.append(error); done.set()
    port.reset_input_buffer()
    reader = threading.Thread(target=receive, name='attn-uart-reader')
    reader.start()
    try:
        port.write(payload); port.flush(); deadline = time.monotonic() + timeout
        while not done.wait(0.05):
            temperature()
            if time.monotonic() >= deadline: raise TimeoutError('no attention completion')
        if errors: raise errors[0]
    finally:
        stop.set(); reader.join(timeout=2)
        if reader.is_alive(): raise RuntimeError('UART reader did not stop; use a finite serial timeout')


def trace_runs(reference, payload, paired=False):
    """Plan a fresh full run and optional result run on unchanged DDR inputs."""
    run = reference.get('run', 1)
    positions = reference['positions']
    trace = reference.get('trace', 'full')
    if (reference.get('profile') != 'gf16-attn-v1' or trace not in ('full', 'result')
            or not 0 < run < 2**32 or not 1 <= positions <= 8):
        raise ValueError('invalid attention reference profile/run/positions/trace')
    magic = 0x41545431
    expected = run | (magic << 32) | (positions << 64) | ((trace == 'result') << 96)
    if payload != expected.to_bytes(16, 'little'):
        raise ValueError('attention descriptor differs from reference')
    first = ('', reference, run, payload)
    if not paired:
        return [first]
    if trace != 'full' or run == 2**32 - 1:
        raise ValueError('trace pair requires full attention vectors and a spare run ID')
    following = {**reference, 'run': run + 1, 'trace': 'result'}
    descriptor = ((run + 1) | (magic << 32) | (positions << 64) | (1 << 96)).to_bytes(16, 'little')
    return [first, ('result-only', following, run + 1, descriptor)]


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--vectors', type=Path, required=True)
    ap.add_argument('--boot', type=Path, required=True)
    ap.add_argument('--output', type=Path, required=True)
    ap.add_argument('--port', required=True); ap.add_argument('--cable', required=True)
    ap.add_argument('--max-temp', type=float, default=70)
    ap.add_argument('--design-hz', type=int, default=60000000)
    ap.add_argument('--capture-baud', type=int, choices=(115200, 230400, 460800, 921600), default=460800)
    ap.add_argument('--trace-pair', action='store_true',
                    help='run full then result-only on the same newly uploaded/readback-verified inputs')
    args = ap.parse_args()
    out = args.output; out.mkdir(parents=True, exist_ok=False)
    boot = json.loads(args.boot.read_text())
    if not boot['checks'].get('gf16_attn_header') or not all(boot['checks'].values()):
        raise ValueError('passing attention boot evidence required')
    if args.design_hz != 60000000: raise ValueError('this experiment qualifies the 60 MHz configuration only')
    manifest = json.loads((args.vectors / 'inputs.json').read_text())
    ref = json.loads((args.vectors / 'reference.json').read_text())
    run_id = ref.get('run', 1)
    positions = ref['positions']
    av.validate_board_inputs(args.vectors, manifest, ref['stages'], run_id, positions,
                             result_only=ref.get('trace') == 'result')
    payload = (args.vectors / manifest['doorbell']['file']).read_bytes()
    planned = trace_runs(ref, payload, args.trace_pair)
    (out / 'command.json').write_text(json.dumps({'argv': sys.argv,
        'bitstream_sha256': boot['bitstream_sha256'],
        'vectors': str(args.vectors.resolve()), 'boot': str(args.boot.resolve()),
        'tool_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        'tool_commit': subprocess.check_output(['git', '-C', str(ROOT), 'rev-parse', 'HEAD'],
                                                text=True).strip()}, indent=2) + '\n')
    last_check = 0

    def temperature(force=False):
        nonlocal last_check
        if not force and time.monotonic() - last_check < 10: return
        done = subprocess.run(['openFPGALoader', '-c', args.cable, '--read-xadc'],
                              text=True, capture_output=True, timeout=15)
        text = done.stdout + done.stderr; match = re.search(r'"temp"\s*:\s*([0-9.]+)', text)
        value = float(match[1]) if match else None
        record = {'utc': datetime.datetime.now(datetime.timezone.utc).isoformat(),
                  'returncode': done.returncode, 'temperature_c': value, 'output': text}
        with (out / 'thermal.jsonl').open('a') as log: log.write(json.dumps(record) + '\n')
        if done.returncode or value is None or value >= args.max_temp:
            raise RuntimeError('XADC check failed or temperature limit reached')
        last_check = time.monotonic(); print(f'XADC {value:.2f} C', flush=True)
    temperature(True)
    dna = subprocess.check_output(['openFPGALoader', '-c', args.cable, '--read-dna'],
                                  text=True, stderr=subprocess.STDOUT)
    (out / 'identity.txt').write_text(dna)
    if boot['run']['dna'] not in dna: raise ValueError('board DNA differs from boot')
    spec = importlib.util.spec_from_file_location('ffn_uart', ROOT / 'tools/fpga-uart-loader.py')
    loader_module = importlib.util.module_from_spec(spec); spec.loader.exec_module(loader_module)

    def rate(before, after, label):
        settings = SimpleNamespace(first_seq=1, guard=0.15, ack_timeout=0.5, max_attempts=6,
                                    garbage_bytes=64, design_hz=args.design_hz)
        link = loader_module.Link(args.port, before, time.monotonic()); record = {}
        try:
            loader = loader_module.Loader(link, settings, random.Random(92))
            record['before'] = loader.status()
            if record['before'] is None: raise RuntimeError('no status at initial baud')
            record['switch'] = loader_module.back_to_default(loader, SimpleNamespace(baud=after), before)
            record['after'] = loader.status()
            if not record['switch']['switched'] or record['after'] is None:
                raise RuntimeError('UART rate switch not confirmed')
            if record['after']['baud_div'] != round(args.design_hz / after): raise RuntimeError('wrong baud divisor')
            if not proto.decode_calib(record['after']['calib'])['calib_complete']: raise RuntimeError('DDR3 not ready')
        finally:
            link.close(); raw = bytes(link.rx); (out / (label + '.rx.bin')).write_bytes(raw)
            record['rx_sha256'] = hashlib.sha256(raw).hexdigest()
            (out / (label + '.json')).write_text(json.dumps(record, indent=2) + '\n')

    def load(path, address, name, margin=0):
        cmd = [sys.executable, str(ROOT / 'tools/fpga-uart-loader.py'), '--port', args.port,
               '--baud', '921600', '--payload', str(path), '--addr', str(address), '--chunk', '2048',
               '--margin', str(margin), '--design-hz', str(args.design_hz),
               '--output', str(out / (name + '.json'))]
        (out / (name + '-command.json')).write_text(json.dumps(cmd, indent=2) + '\n')
        with (out / (name + '.log')).open('w') as log:
            child = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT)
            deadline = time.monotonic() + 1200
            try:
                while child.poll() is None:
                    temperature()
                    if time.monotonic() > deadline: raise TimeoutError('UART transfer timeout')
                    try: child.wait(timeout=1)
                    except subprocess.TimeoutExpired: pass
                if child.returncode: raise RuntimeError('UART load/readback failed: ' + name)
            finally:
                if child.poll() is None:
                    child.terminate()
                    try: child.wait(timeout=5)
                    except subprocess.TimeoutExpired: child.kill(); child.wait()
        receipt = json.loads((out / (name + '.json')).read_text())
        if not receipt['pass'] or not all(receipt['checks'].values()):
            raise RuntimeError('invalid readback receipt')
    with restoring_baud(rate) as switch:
        switch(115200, 921600, 'uart-fast')
        qualify = out / 'qualification.bin'; qualify.write_bytes(bytes(range(256)) * 128)
        load(qualify, 0x2000000, 'qualification', 32)
        for name in ('scales', 'w_in', 'w_sub', 'x', 'rope', 'q', 'k', 'v', 'o'):
            region = manifest[name]; print('Loading', name, region['bytes'], flush=True)
            load(args.vectors / region['file'], region['byte_address'], 'load-' + name)
        if args.capture_baud != 921600:
            switch(921600, args.capture_baud, 'uart-capture')
        import serial
        results = []
        for name, reference, current_run, descriptor in planned:
            folder = out / name; folder.mkdir(exist_ok=True)
            if name:
                # Only the descriptor changes. No intervening upload, reset or
                # FPGA reconfiguration occurs between the two captures.
                (folder / 'reference.json').write_text(json.dumps(reference, indent=1) + '\n')
                (folder / 'doorbell.bin').write_bytes(descriptor)
            temperature(True)
            raw = bytearray()
            try:
                with serial.Serial(args.port, args.capture_baud, timeout=0.05) as port:
                    capture_stream(port, proto.load_frame(1, manifest['doorbell']['byte_address'], descriptor),
                                   raw, current_run, temperature,
                                   timeout=1200 if reference.get('trace') == 'full' else 1800)
            finally:
                (folder / 'capture.txt').write_bytes(raw)
            if not fv.doorbell_acknowledged(bytes(raw)):
                raise RuntimeError('doorbell load acknowledgement absent')
            result = av.validate(bytes(raw), reference['stages'], current_run,
                                 positions=positions, result_only=reference.get('trace') == 'result')
            result.update(bitstream_sha256=boot['bitstream_sha256'], doorbell_ack=True,
                          capture_baud=args.capture_baud, positions=positions)
            if name:
                result['input_reuse'] = {'source': '../', 'first_run': run_id,
                    'manifest_sha256': hashlib.sha256((args.vectors / 'inputs.json').read_bytes()).hexdigest(),
                    'doorbell_sha256': hashlib.sha256(descriptor).hexdigest(),
                    'scope': 'same freshly uploaded/readback-verified inputs; only descriptor changed'}
            results.append((folder, result))
    temperature(True)
    for folder, result in results:
        (folder / 'result.json').write_text(json.dumps(result, indent=2) + '\n'); print(json.dumps(result), flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
