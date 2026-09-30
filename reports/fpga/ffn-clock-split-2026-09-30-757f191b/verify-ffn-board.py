"""Replay retained FFN board captures and raw CRC-checked DDR readback."""
import argparse, gzip, hashlib, json, pathlib, re, sys
ROOT=pathlib.Path('/Users/ssdm4/TRINITY_TRANSFER_2026-09-29/trinity-memory')
BASE=pathlib.Path(__file__).resolve().parent
sys.path.insert(0,str(ROOT/'tools'))
import ffn_vectors as fv
import uart_loader_protocol as proto
ap=argparse.ArgumentParser();ap.add_argument('name');args=ap.parse_args()
folder=BASE/('ffn-board-'+args.name); vectors=pathlib.Path('/Volumes/SSDD/TRINITY_RESULTS_2026-09-29/stages')/('ffn-vectors-'+args.name)
result=json.loads((folder/'result.json').read_text());ref=json.loads((vectors/'reference.json').read_text())
manifest=json.loads((vectors/'inputs.json').read_text());cmd=json.loads((folder/'command.json').read_text())
bp=pathlib.Path(cmd['boot']); boot=json.loads((bp if bp.is_absolute() else ROOT/bp).read_text())
assert all(boot['checks'].values()) and boot['checks']['ffn_header']
assert boot['run']['dna']=='0x00389c0c2d85e85c' and int(boot['run']['idcode'],16)==0x3636093
assert result['bitstream_sha256']==boot['bitstream_sha256']==cmd['bitstream_sha256']
raw=(folder/'capture.txt').read_bytes();checked=fv.validate(raw,ref['expected'],ref['saturations'],ref['run'])
assert fv.doorbell_acknowledged(raw)
for key,value in checked.items(): assert result[key]==value,(key,result[key],value)
# Independently combine signed halves and compare every scalar.
lines=[(t.decode(),int(a,16),int(b,16)) for t,a,b in re.findall(rb'([A-Za-z])([0-9a-fA-F]{8})([0-9a-fA-F]{10})\n',raw)]
for stage in fv.STAGES:
 low=[v for t,i,v in lines if t==stage]; high=[v for t,i,v in lines if t==('J' if stage=='a' else stage.upper())]
 got=[lo|(hi<<32) for lo,hi in zip(low,high)];got=[v-(1<<64) if v>>63 else v for v in got]
 assert got==ref['expected'][stage],stage
# Clock split straight from the raw lines: exactly c then k0, k1.
c=[b for t,a,b in lines if t=='c']; k=[(a,b) for t,a,b in lines if t=='k']
assert len(c)==1 and [a for a,_ in k]==[0,1]
split={'total':c[0],'report_wait':k[0][1],'memory_wait':k[1][1],'compute':c[0]-k[0][1]-k[1][1]}
assert split['compute']>0 and split==result['clock_split'],split
readbacks={}
for name in ('qualification','gate','up','down','scales','post','sub','x'):
 label='qualification' if name=='qualification' else 'load-'+name
 receipt=json.loads((folder/(label+'.json')).read_text())
 assert receipt['pass'] and all(receipt['checks'].values()) and receipt['baud']==921600
 expected=(folder/'qualification.bin').read_bytes() if name=='qualification' else (vectors/manifest[name]['file']).read_bytes()
 address=0x2000000 if name=='qualification' else manifest[name]['byte_address']
 assert receipt['addr']==address and receipt['transfer']['payload_bytes']==len(expected)
 if name!='qualification': assert hashlib.sha256(expected).hexdigest()==manifest[name]['sha256']
 rr=receipt['rx_raw']; received=gzip.decompress((folder/rr['file']).read_bytes())
 assert len(received)==rr['bytes'] and hashlib.sha256(received).hexdigest()==rr['sha256_uncompressed']
 decoder=proto.StreamDecoder(); got=bytearray(len(expected)); seen=bytearray(len(expected)); count=0; rejected=[]
 for off in range(0,len(received),65536):
  for event in decoder.feed(received[off:off+65536]):
   if event.kind!='readback' or not address<=event.addr<address+len(expected): continue
   # A frame that fails CRC was corrupted on the way back to the host; the host
   # re-requested it. It is recorded, never used; coverage below still needs every byte.
   if not event.crc_ok: rejected.append(event.addr); continue
   offset=event.addr-address; data=event.data;assert offset+len(data)<=len(expected)
   assert data==expected[offset:offset+len(data)]
   got[offset:offset+len(data)]=data;seen[offset:offset+len(data)]=b'\1'*len(data);count+=1
 assert all(seen) and bytes(got)==expected,(name,sum(seen),len(expected))
 readbacks[name]={'bytes':len(expected),'decoded_crc_valid_blocks':count,'sha256':hashlib.sha256(expected).hexdigest(),'raw_sha256':rr['sha256_uncompressed'],'retransmits':receipt['transfer']['retransmits'],'crc_rejected_readback_frames':rejected}
 print(name,'raw readback PASS',len(expected),flush=True)
for label,divisor in [('uart-fast',65),('uart-restored',521)]:
 r=json.loads((folder/(label+'.json')).read_text());assert r['switch']['switched'] and r['after']['baud_div']==divisor
 assert hashlib.sha256((folder/(label+'.rx.bin')).read_bytes()).hexdigest()==r['rx_sha256']
t=[json.loads(line) for line in (folder/'thermal.jsonl').read_text().splitlines()]
assert t and all(x['returncode']==0 and 0<x['temperature_c']<70 for x in t)
checked.update(bitstream_sha256=boot['bitstream_sha256'],readbacks=readbacks,temperatures_c={'min':min(x['temperature_c'] for x in t),'max':max(x['temperature_c'] for x in t),'samples':len(t)},uart_restored=115200,source_commit='757f191b9c0b41b5c5bf92b2bd0660e567aa8a18',clock_split=split,tool_commit=cmd['tool_commit'])
path=BASE/('stage-6-verified-'+args.name+'.json');assert not path.exists();path.write_text(json.dumps(checked,indent=2)+'\n')
print('PASS',path,flush=True)
