"""Python representations for the canonical codecs implemented in t27."""
from dataclasses import dataclass
from typing import Iterable
from . import _native as n

class CodecError(ValueError):
    """Invalid values, incompatible sparsity, or noncanonical payload."""

@dataclass(frozen=True)
class Codec:
    id: int
    name: str
    group_size: int
    group_bits: int
    max_nonzero: int | None = None

CODECS = {c.name: c for c in (Codec(0,"baseline2",4,8), Codec(1,"dense5",5,8),
    Codec(2,"dense17",17,27), Codec(3,"dense22",22,35), Codec(4,"sparse41",4,4,1), Codec(5,"sparse82",8,8,2))}
CODECS_BY_ID = {c.id:c for c in CODECS.values()}
LANE_ENCODE = {0:0,1:1,-1:2}
LANE_DECODE = {0:0,1:1,2:-1}

def get_codec(name: str) -> Codec:
    try: return CODECS[name]
    except (KeyError,TypeError): raise CodecError(f"unknown codec: {name!r}") from None

def validate_trits(values: Iterable[int]) -> list[int]:
    items, data = n.integer_array(values, error=CodecError)
    if n.call("tm_api_validate_trits",n.C.c_int32,[n.I32,n.SZ],data,len(items)):
        raise CodecError("weights must be integer -1, 0, or 1")
    return items

def payload_size(count: int, codec: str="dense5") -> int:
    try: n.checked_size(count,"count")
    except ValueError as exc: raise CodecError(str(exc)) from exc
    size=n.call("tm_payload_size",n.C.c_int64,[n.C.c_int32,n.C.c_uint64],get_codec(codec).id,count)
    if size<0: raise CodecError("payload size exceeds native bounds")
    return size

def pack(values: Iterable[int], codec: str="dense5") -> bytes:
    c=get_codec(codec); items,data=n.integer_array(values,error=CodecError)
    capacity=payload_size(len(items),codec); out=n.buffer(capacity)
    size=n.call("tm_encode",n.C.c_int64,[n.C.c_int32,n.I32,n.SZ,n.U8,n.SZ],c.id,data,len(items),out,capacity)
    if size<0: raise CodecError(f"invalid trits or incompatible {codec} sparsity (native {size})")
    return bytes(out[:size])

def unpack(payload: bytes,count: int,codec: str="dense5") -> list[int]:
    c=get_codec(codec); expected=payload_size(count,codec)
    if not isinstance(payload,(bytes,bytearray)): raise CodecError("payload must be bytes or bytearray")
    if len(payload)!=expected: raise CodecError(f"payload length {len(payload)} != expected {expected}")
    data=n.octets(payload); out=(n.C.c_int32*max(1,count))()
    status=n.call("tm_decode",n.C.c_int32,[n.C.c_int32,n.U8,n.SZ,n.SZ,n.I32,n.SZ],c.id,data,len(payload),count,out,count)
    if status: raise CodecError(f"noncanonical payload: invalid code, nonzero padded trits or unused high bits (native {status})")
    return list(out[:count])

def __getattr__(name):
    # Materialize public lookup tables from native decoding; no Python codec logic.
    if name not in ("SPARSE_STATES","SPARSE_CODES"): raise AttributeError(name)
    states={}
    for c in CODECS.values():
        if c.max_nonzero is not None:
            states[c.name]=tuple(tuple(n.call("tm_word_trit",n.C.c_int32,[n.C.c_int32,n.C.c_uint64,n.SZ],c.id,word,lane)
                for lane in range(c.group_size)) for word in range(1<<c.group_bits)
                if n.call("tm_word_valid",n.C.c_bool,[n.C.c_int32,n.C.c_uint64],c.id,word))
    globals()["SPARSE_STATES"]=states
    globals()["SPARSE_CODES"]={key:{state:i for i,state in enumerate(value)} for key,value in states.items()}
    return globals()[name]
