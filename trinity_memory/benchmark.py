"""FFI for the reproducible native t27 synthetic storage experiment."""
import json
from . import _native as n

def run_benchmark(count=65536,repeats=3,seed=27):
    n.checked_size(count,"count",1); n.checked_size(repeats,"repeats",1)
    _,seeds=n.integer_array([seed],bits=64)
    out=n.buffer(4<<20)
    size=n.call("tm_exp_benchmark",n.C.c_int64,[n.SZ,n.SZ,n.C.c_int64,n.U8,n.SZ],count,repeats,seeds[0],out,len(out))
    if size<0: raise ValueError(f"Native benchmark failed ({size})")
    return json.loads(bytes(out[:size]))
