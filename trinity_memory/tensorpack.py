"""Python Tensor objects backed by the native t27 TensorPack implementation."""
from collections.abc import Sequence
from dataclasses import asdict,dataclass
import json
import struct
from . import _native as n
from .codecs import CodecError

class TensorPackError(CodecError): pass
@dataclass(frozen=True)
class Tensor:
    name: str
    shape: tuple[int,...]
    values: tuple[int,...]
    codec: str="dense5"
    scales: tuple[float,...]=(1.0,)
    scale_axis: int|None=None
    axes: tuple[str,...]=()
MAGIC=b"TTPK"
VERSION=1
PREFIX=struct.Struct("<4sBBHIIQ")
CHECKSUMS=struct.Struct("<II")
HEADER_BYTES=32
MAX_TENSORS=1024
MAX_METADATA_BYTES=1<<20
MAX_PAYLOAD_BYTES=64<<20
MAX_TOTAL_TRITS=4<<20
MAX_RANK=16
MAX_DIMENSION=(1<<31)-1
MAX_NAME_BYTES=256
MAX_AXIS_BYTES=64
MAX_JSON_DEPTH=8

def encode_tensors(tensors):
    if not isinstance(tensors,Sequence) or isinstance(tensors,(str,bytes,bytearray)):
        raise TensorPackError("tensors must be a sequence of Tensor instances")
    # Python-specific representation requirements do not survive JSON conversion.
    for t in tensors:
        if not isinstance(t,Tensor): raise TensorPackError("every item must be a Tensor instance")
        if any(type(getattr(t,key)) is not tuple for key in ("shape","values","scales","axes")):
            raise TensorPackError("shape, values, scales and axes must be tuples")
    try: source=n.json_bytes([asdict(t) for t in tensors])
    except (TypeError,ValueError,OverflowError,UnicodeError) as exc: raise TensorPackError(str(exc)) from exc
    capacity=HEADER_BYTES+MAX_METADATA_BYTES+MAX_PAYLOAD_BYTES
    work=n.tensor_workspace(max(len(source),MAX_METADATA_BYTES),MAX_TOTAL_TRITS)
    values=(n.C.c_int32*MAX_TOTAL_TRITS)(); metadata=n.buffer(MAX_METADATA_BYTES); out=n.buffer(capacity); data=n.octets(source)
    try:
        size=n.call("tm_tp_encode_input_json",n.C.c_int64,[n.U8,n.SZ,n.C.c_void_p,n.I32,n.SZ,n.U8,n.SZ,n.U8,n.SZ],data,len(source),work,values,MAX_TOTAL_TRITS,metadata,MAX_METADATA_BYTES,out,capacity)
        if size<0: raise TensorPackError(f"invalid TensorPack input (native {size})")
        return bytes(out[:size])
    finally: n.tensor_free(work)

def _read(data,max_total_trits,inspect):
    try: n.checked_size(max_total_trits,"max_total_trits",1)
    except ValueError as exc: raise TensorPackError(str(exc)) from exc
    if not isinstance(data,(bytes,bytearray)): raise TensorPackError("TensorPack must be bytes or bytearray")
    limit=min(max_total_trits,MAX_TOTAL_TRITS); source=n.octets(data)
    work=n.tensor_workspace(min(len(data),MAX_METADATA_BYTES)+1,limit)
    capacity=MAX_METADATA_BYTES*2 if inspect else MAX_TOTAL_TRITS*4+MAX_METADATA_BYTES*2
    out=n.buffer(capacity)
    try:
        if inspect:
            size=n.call("tm_tp_inspect_json",n.C.c_int64,[n.U8,n.SZ,n.C.c_void_p,n.C.c_uint64,n.U8,n.SZ],source,len(data),work,limit,out,capacity)
        else:
            values=(n.C.c_int32*max(1,limit))()
            size=n.call("tm_tp_export_json",n.C.c_int64,[n.U8,n.SZ,n.C.c_void_p,n.I32,n.SZ,n.C.c_uint64,n.U8,n.SZ],source,len(data),work,values,limit,limit,out,capacity)
        if size<0: raise TensorPackError(f"invalid TensorPack framing, metadata, CRC or values (native {size})")
        result=json.loads(bytes(out[:size]))
        if inspect: return result
        return [Tensor(t["name"],tuple(t["shape"]),tuple(t["values"]),t["codec"],tuple(t["scales"]),t["scale_axis"],tuple(t["axes"])) for t in result]
    finally: n.tensor_free(work)

def decode_tensors(data,*,max_total_trits=MAX_TOTAL_TRITS): return _read(data,max_total_trits,False)
def inspect_tensorpack(data,*,max_total_trits=MAX_TOTAL_TRITS): return _read(data,max_total_trits,True)
