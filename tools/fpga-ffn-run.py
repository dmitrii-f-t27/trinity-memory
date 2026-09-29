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
import time
from types import SimpleNamespace

import ffn_vectors as fv
import uart_loader_protocol as proto

ROOT=Path(__file__).resolve().parents[1]


def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--vectors',type=Path,required=True)
    ap.add_argument('--boot',type=Path,required=True)
    ap.add_argument('--output',type=Path,required=True)
    ap.add_argument('--port',required=True); ap.add_argument('--cable',required=True)
    ap.add_argument('--max-temp',type=float,default=70)
    ap.add_argument('--design-hz',type=int,default=60000000)
    args=ap.parse_args(); out=args.output; out.mkdir(parents=True,exist_ok=False)
    boot=json.loads(args.boot.read_text())
    if not boot['checks'].get('ffn_header') or not all(boot['checks'].values()):
        raise ValueError('passing FFN boot evidence required')
    if args.design_hz != 60000000: raise ValueError('this experiment qualifies the 60 MHz configuration only')
    manifest=json.loads((args.vectors/'inputs.json').read_text())
    ref=json.loads((args.vectors/'reference.json').read_text())
    for region in manifest.values():
        data=(args.vectors/region['file']).read_bytes()
        if len(data)!=region['bytes'] or hashlib.sha256(data).hexdigest()!=region['sha256']:
            raise ValueError('input payload hash mismatch')
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
    rate(115200,921600,'uart-fast')
    qualify=out/'qualification.bin'; qualify.write_bytes(bytes(range(256))*128)
    load(qualify,0x2000000,'qualification',32)
    for name in ('gate','up','down','scales','post','sub','x'):
        region=manifest[name]; print('Loading',name,region['bytes'],flush=True)
        load(args.vectors/region['file'],region['byte_address'],'load-'+name)
    import serial
    raw=bytearray(); complete=False; acked=False
    payload=(args.vectors/manifest['doorbell']['file']).read_bytes()
    try:
        with serial.Serial(args.port,921600,timeout=0.05) as port:
            port.reset_input_buffer(); deadline=time.monotonic()+180
            port.write(proto.load_frame(1,manifest['doorbell']['byte_address'],payload)); port.flush()
            while time.monotonic()<deadline:
                temperature(); raw.extend(port.read(max(1,port.in_waiting)))
                if re.search(rb'z'+f'{ref["run"]:08x}'.encode()+rb'0000000000\n',raw):
                    complete=True; break
                if re.search(rb'E[0-9a-fA-F]{18}\n',raw): raise RuntimeError('FFN fatal error in capture')
    finally:
        (out/'capture.txt').write_bytes(raw)
    if not complete: raise TimeoutError('no FFN completion')
    # Loader acknowledgement is a separate CRC-checked protocol line. J is the
    # FFN a-high tag, so the acknowledgement cannot alias a numerical result.
    acked=fv.doorbell_acknowledged(bytes(raw))
    if not acked: raise RuntimeError('doorbell load acknowledgement absent')
    result=fv.validate(bytes(raw),ref['expected'],ref['saturations'],ref['run'])
    rate(921600,115200,'uart-restored'); temperature(True)
    result.update(bitstream_sha256=boot['bitstream_sha256'],doorbell_ack=True,f64_error=ref['f64_error'])
    (out/'result.json').write_text(json.dumps(result,indent=2)+'\n'); print(json.dumps(result)); return 0


if __name__=='__main__':
    raise SystemExit(main())
