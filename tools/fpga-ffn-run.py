"""Issue #92: load, read back, run and capture the full FFN on a pre-booted board.

Requires a passing hash-checked --ffn boot report. Checks XADC every ten seconds
during transfers and capture. No bitstream loading or flash write occurs here.
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

import ffn_vectors as fv
import uart_loader_protocol as proto
from uart_session import restoring_baud

ROOT=Path(__file__).resolve().parents[1]


def capture_stream(port, payload, raw, run_id, temperature, timeout=180):
    """Drain UART continuously, including while the main thread checks XADC."""
    stop=threading.Event(); done=threading.Event(); errors=[]
    finish=re.compile(rb'z'+f'{run_id:08x}'.encode()+rb'0000000000\n')
    fatal=re.compile(rb'E[0-9a-fA-F]{18}\n')
    def receive():
        tail=b''
        try:
            while not stop.is_set():
                chunk=port.read(4096)
                if not chunk: continue
                raw.extend(chunk)
                # Scan only new bytes and a partial line, never the whole
                # growing capture on every byte (quadratic at high baud).
                window=tail+chunk;tail=window[-20:]
                if fatal.search(window):
                    errors.append(RuntimeError('FFN fatal error in capture'));done.set();return
                if finish.search(window): done.set();return
        except Exception as error:
            errors.append(error);done.set()
    port.reset_input_buffer()
    reader=threading.Thread(target=receive,name='ffn-uart-reader')
    reader.start()
    try:
        port.write(payload);port.flush();deadline=time.monotonic()+timeout
        while not done.wait(0.05):
            temperature()
            if time.monotonic()>=deadline: raise TimeoutError('no FFN completion')
        if errors: raise errors[0]
    finally:
        stop.set();reader.join(timeout=2)
        if reader.is_alive(): raise RuntimeError('UART reader did not stop; use a finite serial timeout')


def trace_runs(reference, payload, paired=False):
    """Plan a fresh full run and optional result run using unchanged DDR inputs."""
    run = reference.get('run', 1)
    first = ('', reference, run, payload)
    if not paired:
        return [first]
    if (reference.get('profile') != 'gf16-ffn-v1' or
            reference.get('trace', 'full') != 'full' or not 0 < run < 2**32-1):
        raise ValueError('trace pair requires full GF16 vectors and a spare run ID')
    magic = 0x47464631
    if payload != (run | (magic << 32)).to_bytes(16, 'little'):
        raise ValueError('trace pair initial descriptor differs')
    following = {**reference, 'run':run+1, 'trace':'result'}
    descriptor = ((run+1) | (magic << 32) | (1 << 64)).to_bytes(16, 'little')
    return [first, ('result-only', following, run+1, descriptor)]


def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--vectors',type=Path,required=True)
    ap.add_argument('--boot',type=Path,required=True)
    ap.add_argument('--output',type=Path,required=True)
    ap.add_argument('--port',required=True); ap.add_argument('--cable',required=True)
    ap.add_argument('--max-temp',type=float,default=70)
    ap.add_argument('--design-hz',type=int,default=60000000)
    ap.add_argument('--capture-baud',type=int,choices=(115200,230400,460800,921600),default=460800,
                    help='continuous result stream baud; verified uploads still use921600 (default460800)')
    ap.add_argument('--trace-pair',action='store_true',help='GF16: run full then result-only on the same newly uploaded/readback-verified inputs')
    ap.add_argument('--gf16-ffn',action='store_true',help='require GF16 boot, vectors and stage protocol')
    args=ap.parse_args()
    if args.trace_pair and not args.gf16_ffn:ap.error('--trace-pair requires --gf16-ffn')
    out=args.output; out.mkdir(parents=True,exist_ok=False)
    boot=json.loads(args.boot.read_text())
    header='gf16_ffn_header' if args.gf16_ffn else 'ffn_header'
    if not boot['checks'].get(header) or not all(boot['checks'].values()):
        raise ValueError('passing FFN boot evidence required')
    if args.design_hz != 60000000: raise ValueError('this experiment qualifies the 60 MHz configuration only')
    manifest=json.loads((args.vectors/'inputs.json').read_text())
    ref=json.loads((args.vectors/'reference.json').read_text())
    run_id=ref.get('run',1) if args.gf16_ffn else ref['run']
    if args.gf16_ffn:
        sys.path.insert(0,str(ROOT))
        from tools import gf16_ffn_vectors as gf_vectors
        gf_vectors.validate_board_inputs(args.vectors, manifest, ref, run_id)
    for region in manifest.values():
        data=(args.vectors/region['file']).read_bytes()
        if len(data)!=region['bytes'] or hashlib.sha256(data).hexdigest()!=region['sha256']:
            raise ValueError('input payload hash mismatch')
    payload=(args.vectors/manifest['doorbell']['file']).read_bytes()
    planned=trace_runs(ref,payload,args.trace_pair)
    (out/'command.json').write_text(json.dumps({'argv':sys.argv,'bitstream_sha256':boot['bitstream_sha256'],
        'vectors':str(args.vectors.resolve()),'boot':str(args.boot.resolve()),
        'tool_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        'tool_commit':subprocess.check_output(['git','-C',str(ROOT),'rev-parse','HEAD'],text=True).strip()},indent=2)+'\n')
    last_check=0
    def temperature(force=False):
        nonlocal last_check
        if not force and time.monotonic()-last_check<10: return
        done=subprocess.run(['openFPGALoader','-c',args.cable,'--read-xadc'],text=True,capture_output=True,timeout=15)
        text=done.stdout+done.stderr; match=re.search(r'"temp"\s*:\s*([0-9.]+)',text)
        value=float(match[1]) if match else None
        record={'utc':datetime.datetime.now(datetime.timezone.utc).isoformat(),'returncode':done.returncode,
                'temperature_c':value,'output':text}
        with (out/'thermal.jsonl').open('a') as log: log.write(json.dumps(record)+'\n')
        if done.returncode or value is None or value>=args.max_temp:
            raise RuntimeError('XADC check failed or temperature limit reached')
        last_check=time.monotonic(); print(f'XADC {value:.2f} C',flush=True)
    temperature(True)
    dna=subprocess.check_output(['openFPGALoader','-c',args.cable,'--read-dna'],text=True,stderr=subprocess.STDOUT)
    (out/'identity.txt').write_text(dna)
    if boot['run']['dna'] not in dna: raise ValueError('board DNA differs from boot')
    spec=importlib.util.spec_from_file_location('ffn_uart',ROOT/'tools/fpga-uart-loader.py')
    loader_module=importlib.util.module_from_spec(spec); spec.loader.exec_module(loader_module)
    def rate(before,after,label):
        settings=SimpleNamespace(first_seq=1,guard=0.15,ack_timeout=0.5,max_attempts=6,garbage_bytes=64,
                                 design_hz=args.design_hz)
        link=loader_module.Link(args.port,before,time.monotonic()); record={}
        try:
            loader=loader_module.Loader(link,settings,random.Random(92))
            record['before']=loader.status()
            if record['before'] is None: raise RuntimeError('no status at initial baud')
            record['switch']=loader_module.back_to_default(loader,SimpleNamespace(baud=after),before)
            record['after']=loader.status()
            if not record['switch']['switched'] or record['after'] is None:
                raise RuntimeError('UART rate switch not confirmed')
            if record['after']['baud_div']!=round(args.design_hz/after): raise RuntimeError('wrong baud divisor')
            if not proto.decode_calib(record['after']['calib'])['calib_complete']: raise RuntimeError('DDR3 not ready')
        finally:
            link.close(); raw=bytes(link.rx); (out/(label+'.rx.bin')).write_bytes(raw)
            record['rx_sha256']=hashlib.sha256(raw).hexdigest()
            (out/(label+'.json')).write_text(json.dumps(record,indent=2)+'\n')
    def load(path,address,name,margin=0):
        cmd=[sys.executable,str(ROOT/'tools/fpga-uart-loader.py'),'--port',args.port,'--baud','921600',
             '--payload',str(path),'--addr',str(address),'--chunk','2048','--margin',str(margin),
             '--design-hz',str(args.design_hz),'--output',str(out/(name+'.json'))]
        (out/(name+'-command.json')).write_text(json.dumps(cmd,indent=2)+'\n')
        with (out/(name+'.log')).open('w') as log:
            child=subprocess.Popen(cmd,stdout=log,stderr=subprocess.STDOUT)
            deadline=time.monotonic()+600
            try:
                while child.poll() is None:
                    temperature()
                    if time.monotonic()>deadline: raise TimeoutError('UART transfer timeout')
                    try: child.wait(timeout=1)
                    except subprocess.TimeoutExpired: pass
                if child.returncode: raise RuntimeError('UART load/readback failed: '+name)
            finally:
                if child.poll() is None:
                    child.terminate()
                    try: child.wait(timeout=5)
                    except subprocess.TimeoutExpired: child.kill(); child.wait()
        receipt=json.loads((out/(name+'.json')).read_text())
        if not receipt['pass'] or not all(receipt['checks'].values()): raise RuntimeError('invalid readback receipt')
    results=[]
    with restoring_baud(rate) as switch:
        switch(115200,921600,'uart-fast')
        qualify=out/'qualification.bin'; qualify.write_bytes(bytes(range(256))*128)
        load(qualify,0x2000000,'qualification',32)
        for name in ('gate','up','down','scales','post','sub','x'):
            region=manifest[name]; print('Loading',name,region['bytes'],flush=True)
            load(args.vectors/region['file'],region['byte_address'],'load-'+name)
        if args.capture_baud!=921600:
            switch(921600,args.capture_baud,'uart-capture')
        import serial
        for name,reference,current_run,descriptor in planned:
            folder=out/name;folder.mkdir(exist_ok=True)
            if name:
                # Only the descriptor changes. No intervening upload, reset or
                # FPGA reconfiguration occurs between the two captures.
                (folder/'reference.json').write_text(json.dumps(reference,indent=1)+'\n')
                (folder/'doorbell.bin').write_bytes(descriptor)
            temperature(True)
            raw=bytearray()
            try:
                with serial.Serial(args.port,args.capture_baud,timeout=0.05) as port:
                    capture_stream(port,proto.load_frame(1,manifest['doorbell']['byte_address'],descriptor),
                                   raw,current_run,temperature)
            finally:
                (folder/'capture.txt').write_bytes(raw)
            if not fv.doorbell_acknowledged(bytes(raw)):
                raise RuntimeError('doorbell load acknowledgement absent')
            result=(gf_vectors.validate(bytes(raw),reference,current_run) if args.gf16_ffn else
                    fv.validate(bytes(raw),reference['expected'],reference['saturations'],current_run))
            result.update(bitstream_sha256=boot['bitstream_sha256'],doorbell_ack=True,capture_baud=args.capture_baud)
            if name:
                result['input_reuse']={'source':'../', 'first_run':run_id,
                    'manifest_sha256':hashlib.sha256((args.vectors/'inputs.json').read_bytes()).hexdigest(),
                    'doorbell_sha256':hashlib.sha256(descriptor).hexdigest(),
                    'scope':'same freshly uploaded/readback-verified inputs; only descriptor changed'}
            if not args.gf16_ffn:result['f64_error']=ref['f64_error']
            results.append((folder,result))
    temperature(True)
    for folder,result in results:
        (folder/'result.json').write_text(json.dumps(result,indent=2)+'\n');print(json.dumps(result),flush=True)
    return 0


if __name__=='__main__':
    raise SystemExit(main())
