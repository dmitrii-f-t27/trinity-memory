"""TMEM v1 representations; validation, CRC and serialization execute in t27."""
import json
import struct
from . import _native as n
from .codecs import CodecError,get_codec,payload_size,CODECS_BY_ID
PREFIX=struct.Struct("<4sBBBBQI")
CRC=struct.Struct("<I")
HEADER_BYTES=24
MAGIC=b"TMEM"
VERSION=1

def encode_file(values,codec="dense5"):
    c=get_codec(codec); items,data=n.integer_array(values,error=CodecError)
    capacity=HEADER_BYTES+payload_size(len(items),codec); out=n.buffer(capacity)
    size=n.call("tm_pack",n.C.c_int64,[n.C.c_int32,n.I32,n.SZ,n.U8,n.SZ],c.id,data,len(items),out,capacity)
    if size<0: raise CodecError(f"invalid TMEM input (native {size})")
    return bytes(out[:size])

def _read(data):
    if not isinstance(data,(bytes,bytearray)): raise CodecError("TMEM must be bytes or bytearray")
    source=n.octets(data)
    count=n.call("tm_api_tmem_count",n.C.c_int64,[n.U8,n.SZ,n.SZ],source,len(data),len(data)*8)
    if count<0: raise CodecError("truncated or invalid TMEM count")
    values=(n.C.c_int32*max(1,count))()
    size=n.call("tm_unpack",n.C.c_int64,[n.U8,n.SZ,n.I32,n.SZ],source,len(data),values,count)
    if size<0:
        if size==-7: raise CodecError("nonzero padded trits or unused high bits")
        if size==-6: raise CodecError(f"invalid {CODECS_BY_ID.get(data[5]).name} code")
        raise CodecError(f"invalid TMEM framing, CRC or payload (native {size})")
    out=n.buffer(2048)
    size=n.call("tm_api_tmem_metadata",n.C.c_int64,[n.U8,n.SZ,n.C.c_uint64,n.U8,n.SZ],source,len(data),count,out,2048)
    if size<0: raise CodecError("native TMEM metadata export failed")
    return json.loads(bytes(out[:size])),list(values[:count])

def decode_file(data): return _read(data)[1]
def inspect_file(data): return _read(data)[0]
