"""Thin FFI for explicit lossy preparation and entropy implemented in t27."""
from . import _native as n
from .codecs import CodecError

def entropy_bpw(values):
    items,data=n.integer_array(values,bits=64,error=CodecError); result=n.C.c_double()
    status=n.call("tm_entropy_trits",n.C.c_int32,[n.I64,n.C.c_uint64,n.F64],data,len(items),n.C.byref(result))
    if status: raise CodecError("entropy requires integer trits")
    return result.value

def symmetric_entropy(zero_fraction):
    _,data=n.real_array([zero_fraction]); result=n.C.c_double()
    status=n.call("tm_symmetric_entropy",n.C.c_int32,[n.C.c_double,n.F64],data[0],n.C.byref(result))
    if status: raise ValueError("zero_fraction must be finite and in [0, 1]")
    return result.value

class _ExactWeight(n.C.Structure):
    _fields_=[("data",n.U8),("size",n.SZ),("real",n.C.c_double),("is_integer",n.C.c_bool)]

def project_topk(values,block_size,max_nonzero):
    n.checked_size(block_size,"block_size",1); n.checked_size(max_nonzero,"max_nonzero")
    items=list(values); records=(_ExactWeight*max(1,len(items)))(); keepalive=[]
    for index,value in enumerate(items):
        if isinstance(value,bool) or not isinstance(value,(int,float)):
            raise CodecError("projection requires finite real weights")
        try: real=float(value)
        except (OverflowError,ValueError) as exc:
            raise CodecError("projection requires a finite real representation") from exc
        integer=isinstance(value,int)
        if integer:
            magnitude=abs(value); size=(magnitude.bit_length()+7)//8
            if size>128: raise CodecError("integer magnitude exceeds the native 128-byte representation bound")
            raw=magnitude.to_bytes(size,"big")
        else: raw=b""
        data=n.octets(raw); keepalive.append(data)
        records[index]=_ExactWeight(data,len(raw),real,integer)
    out=(n.C.c_int64*max(1,len(items)))()
    status=n.call("tm_project_topk_exact",n.C.c_int32,[n.C.POINTER(_ExactWeight),n.C.c_uint64,n.C.c_uint64,n.C.c_uint64,n.I64,n.C.c_uint64],
                  records,len(items),block_size,max_nonzero,out,len(items))
    if status: raise CodecError("invalid top-k bounds or nonfinite real weights")
    return list(out[:len(items)])
