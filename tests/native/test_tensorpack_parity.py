import ctypes as C, copy, json, math, random, struct, zlib
from dataclasses import asdict
from pathlib import Path
import sys
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tests/reference"))
from trinity_memory_reference.tensorpack import Tensor, encode_tensors, decode_tensors, TensorPackError, inspect_tensorpack
ROOT = Path(__file__).resolve().parents[2]
LIBRARY = ROOT / 'build/t27' / ('libtrinity_memory_t27.dylib' if sys.platform == 'darwin' else 'libtrinity_memory_t27.so')
lib=C.CDLL(str(LIBRARY))
U8=C.POINTER(C.c_uint8); SZ=C.c_size_t
class Text(C.Structure): _fields_=[('data',U8),('size',SZ)]
class Descriptor(C.Structure):
    _fields_=[('name',U8),('name_size',SZ),('shape',C.POINTER(C.c_uint64)),('rank',SZ),('scales',C.POINTER(C.c_double)),('scale_count',SZ),('scale_axis',C.c_int32),('axes',C.POINTER(Text)),('axes_count',SZ),('codec',C.c_int32),('offset',C.c_uint64),('length',C.c_uint64)]
class Token(C.Structure):
    _fields_=[('kind',C.c_int32),('first',C.c_int64),('next',C.c_int64),('text',U8),('text_size',SZ),('integer',C.c_int64),('real',C.c_double),('uinteger',C.c_uint64),('negative',C.c_bool),('integer_fits_i64',C.c_bool)]
class Workspace(C.Structure):
    _fields_=[('tokens',C.POINTER(Token)),('token_capacity',SZ),('arena',U8),('arena_capacity',SZ),('items',C.POINTER(Descriptor)),('item_capacity',SZ),('shapes',C.POINTER(C.c_uint64)),('shape_capacity',SZ),('scales',C.POINTER(C.c_double)),('scale_capacity',SZ),('axes',C.POINTER(Text)),('axis_capacity',SZ)]
class Validation(C.Structure):
    _fields_=[('tensor_count',SZ),('total_trits',C.c_uint64),('payload_bytes',SZ),('framing_valid',C.c_bool),('descriptors_valid',C.c_bool),('json_binding_valid',C.c_bool)]
codecs=['baseline2','dense5','dense17','dense22','sparse41','sparse82']
lib.tm_tp_decode_json.argtypes=[U8,SZ,C.POINTER(Workspace),C.POINTER(C.c_int32),SZ,C.c_uint64,C.POINTER(Validation)]
lib.tm_tp_decode_json.restype=C.c_int32
lib.tm_tp_encode_json.argtypes=[C.POINTER(Descriptor),SZ,C.POINTER(C.c_int32),SZ,U8,SZ,U8,SZ]
lib.tm_tp_encode_json.restype=C.c_int64
refs=[(Token*65536)(),(C.c_uint8*1048576)(),(Descriptor*1024)(),(C.c_uint64*16384)(),(C.c_double*65536)(),(Text*16384)()]
work=Workspace(refs[0],len(refs[0]),refs[1],len(refs[1]),refs[2],len(refs[2]),refs[3],len(refs[3]),refs[4],len(refs[4]),refs[5],len(refs[5]))
def native_decode(data, limit=4194304):
    inp=(C.c_uint8*len(data)).from_buffer_copy(data);out=(C.c_int32*8192)(*[777]*8192);r=Validation();C.memset(C.byref(r),0x66,C.sizeof(r));previous=bytes(r)
    status=lib.tm_tp_decode_json(inp,len(inp),C.byref(work),out,len(out),limit,C.byref(r))
    if status:
        assert all(v==777 for v in out) and bytes(r)==previous
        return status,None
    assert r.json_binding_valid and r.framing_valid and r.descriptors_valid
    tensors=[];offset=0
    for i in range(r.tensor_count):
        d=work.items[i];count=math.prod(d.shape[j] for j in range(d.rank))
        tensors.append(Tensor(C.string_at(d.name,d.name_size).decode(),tuple(d.shape[j] for j in range(d.rank)),tuple(out[offset:offset+count]),codecs[d.codec],tuple(d.scales[j] for j in range(d.scale_count)),None if d.scale_axis==-1 else d.scale_axis,tuple(C.string_at(d.axes[j].data,d.axes[j].size).decode() for j in range(d.axes_count))))
        offset+=count
    assert all(v==777 for v in out[offset:])
    return status,tensors

def native_encode(tensors):
    keep=[];ds=(Descriptor*len(tensors))();flat=[]
    for i,t in enumerate(tensors):
        name=t.name.encode();nb=(C.c_uint8*len(name)).from_buffer_copy(name);shape=(C.c_uint64*len(t.shape))(*t.shape);scale=(C.c_double*len(t.scales))(*t.scales);axes=(Text*len(t.axes))()
        for j,a in enumerate(t.axes):
            b=a.encode();ab=(C.c_uint8*len(b)).from_buffer_copy(b);keep.append(ab);axes[j]=Text(ab,len(b))
        keep.extend([nb,shape,scale,axes]);ds[i]=Descriptor(nb,len(name),shape,len(shape),scale,len(scale),-1 if t.scale_axis is None else t.scale_axis,axes,len(axes),codecs.index(t.codec),0,0);flat.extend(t.values)
    values=(C.c_int32*len(flat))(*flat);meta=(C.c_uint8*1048576)();out=(C.c_uint8*2097152)()
    n=lib.tm_tp_encode_json(ds,len(ds),values,len(values),meta,len(meta),out,len(out));assert n>=0,n
    return bytes(out[:n])


class TensorPackParity(unittest.TestCase):
    def test_all_codec_pack_bytes_and_decoded_descriptors(self):
        rng=random.Random(2709);cases=[[],[Tensor('scalar',(),(-1,))]]
        for codec in codecs:
            for n in [1,2,3,4,5,8,16,17,22,32,63,128,511]:
                values=[rng.randrange(-1,2) for _ in range(n)]
                if codec in ('sparse41','sparse82'):
                    group=4 if codec=='sparse41' else 8;maximum=1 if group==4 else 2
                    values=[0]*n
                    for start in range(0,n,group):
                        for p in rng.sample(range(start,min(start+group,n)), min(maximum,n-start)): values[p]=rng.choice((-1,1))
                scales=[]
                while len(scales)<n:
                    v=struct.unpack('d',struct.pack('Q',rng.getrandbits(63)))[0]
                    if math.isfinite(v) and v>0:scales.append(v)
                cases.append([Tensor('name-🧠-"-\\',(n,),tuple(values),codec,tuple(scales),0,('samples',))])
        cases.append([cases[i][0] for i in range(2,8)])
        cases[-1]=[Tensor(str(i)+t.name,t.shape,t.values,t.codec,t.scales,t.scale_axis,t.axes) for i,t in enumerate(cases[-1])]
        for ts in cases:
            expected=encode_tensors(ts);status,actual=native_decode(expected)
            self.assertEqual(status,0);self.assertEqual(actual,ts)
            self.assertEqual(native_encode(ts),expected)

    def test_resealed_schema_mutation_parity_and_atomic_output(self):
        base=encode_tensors([Tensor('w',(1,),(1,))]);m=struct.unpack_from('<I',base,8)[0];metadata=json.loads(base[32:32+m]);payload=base[32+m:]
        def reframe(md,raw=False,count=1):
            b=md if raw else json.dumps(md,separators=(',',':'),ensure_ascii=True).encode()
            prefix=struct.pack('<4sBBHIIQ',b'TTPK',1,0,0,len(b),count,len(payload))
            return prefix+struct.pack('<II',zlib.crc32(b,zlib.crc32(prefix)),zlib.crc32(payload))+b+payload
        mutations=[]
        variants=[None,False,True,0,1,-1,1.0,0.0,'', 'x', [], {},[0],[1],[1.0],[True],['x'],[1,1]]
        for key in metadata['tensors'][0]:
            for value in variants:
                md=copy.deepcopy(metadata);md['tensors'][0][key]=value;mutations.append(reframe(md))
            md=copy.deepcopy(metadata);del md['tensors'][0][key];mutations.append(reframe(md))
        for key in metadata:
            for value in variants:
                md=copy.deepcopy(metadata);md[key]=value;mutations.append(reframe(md))
        md=copy.deepcopy(metadata);md['tensors'][0]['extra']=0;mutations.append(reframe(md))
        for b in [b'{"order":"C","tensors":[]}',b'{}',b'null',b'[]',b'{"order":"C","tensors":[],"order":"C"}',b'{"order":"C","tensors":[],"\\u006frder":"C"}']:
            mutations.append(reframe(b,raw=True))
        for data in mutations:
            try: expected=decode_tensors(data);valid=True
            except TensorPackError: valid=False
            status,actual=native_decode(data)
            self.assertEqual(status==0,valid,repr(data))
            if valid:self.assertEqual(actual,expected)

    def test_200000_ieee_patterns_python_json_presentation(self):
        fn=lib.tm_tpj_format_f64;fn.argtypes=[C.c_double,C.c_void_p,C.c_size_t];fn.restype=C.c_int64
        buf=C.create_string_buffer(128)
        values=[0.0,-0.0,1e-4,1e-5,1e15,1e16,1e23,5e-324,1.7976931348623157e308,float.fromhex('0x1.17dccc80c1ae8p+64')]
        rng=random.Random(27)
        values += [struct.unpack('d',struct.pack('Q',rng.getrandbits(64)))[0] for _ in range(200000)]
        for value in values:
            if not math.isfinite(value):continue
            n=fn(value,buf,len(buf));self.assertGreaterEqual(n,0)
            self.assertEqual(buf.raw[:n].decode(),json.dumps(value,allow_nan=False,separators=(',',':')),value.hex())

    def test_tensor_cli_defaults_export_and_inspect_semantics(self):
        encode=lib.tm_tp_encode_input_json
        encode.argtypes=[U8,SZ,C.POINTER(Workspace),C.POINTER(C.c_int32),SZ,U8,SZ,U8,SZ];encode.restype=C.c_int64
        export=lib.tm_tp_export_json
        export.argtypes=[U8,SZ,C.POINTER(Workspace),C.POINTER(C.c_int32),SZ,C.c_uint64,U8,SZ];export.restype=C.c_int64
        inspect=lib.tm_tp_inspect_json
        inspect.argtypes=[U8,SZ,C.POINTER(Workspace),C.c_uint64,U8,SZ];inspect.restype=C.c_int64
        documents=[[],[{'name':'w','shape':[3],'values':[1,0,-1]}],
            [{'name':'row-🧠','shape':[2],'values':[-1,1],'scales':[0.5,2.0],'scale_axis':0,'axes':['sample']},
             {'name':'scalar','shape':[],'values':[0],'codec':'sparse41'}]]
        baseline={'name':'w','shape':[1],'values':[1]}
        variants=[None,False,True,0,1,-1,1.0,0.0,'','x',[],{},[0],[1],[1.0],[True],['x'],[1,1]]
        for key in ('name','shape','values','codec','scales','scale_axis','axes','extra'):
            for value in variants:
                d=copy.deepcopy(baseline);d[key]=value;documents.append([d])
        for document in documents:
            try:
                tensors=[]
                for entry in document:
                    fields=dict(entry)
                    for name in ('shape','values','scales','axes'):
                        if name in fields:
                            if not isinstance(fields[name],list):raise ValueError('not an array')
                            fields[name]=tuple(fields[name])
                    tensors.append(Tensor(**fields))
                expected=encode_tensors(tensors);valid=True
            except (ValueError,TypeError):valid=False
            raw=json.dumps(document,ensure_ascii=False).encode();inp=(C.c_uint8*len(raw)).from_buffer_copy(raw)
            values=(C.c_int32*8192)();meta=(C.c_uint8*1048576)();out=(C.c_uint8*2097152)();C.memset(out,0xa5,len(out))
            n=encode(inp,len(inp),C.byref(work),values,len(values),meta,len(meta),out,len(out))
            self.assertEqual(n>=0,valid,repr(document))
            if not valid:
                self.assertEqual(bytes(out),b'\xa5'*len(out));continue
            self.assertEqual(bytes(out[:n]),expected)
            text=(C.c_uint8*2097152)()
            size=export(out,n,C.byref(work),values,len(values),4194304,text,len(text));self.assertGreaterEqual(size,0)
            self.assertEqual(json.loads(bytes(text[:size])),json.loads(json.dumps([asdict(t) for t in tensors])))
            size=inspect(out,n,C.byref(work),4194304,text,len(text));self.assertGreaterEqual(size,0)
            self.assertEqual(json.loads(bytes(text[:size])),inspect_tensorpack(expected))

if __name__=='__main__':unittest.main()
