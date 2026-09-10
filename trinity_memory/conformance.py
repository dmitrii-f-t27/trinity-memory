"""Native conformance orchestration; Python only reads files and converts JSON."""
import json
from pathlib import Path
from . import _native as n

def run_conformance(vectors:Path,*,rtl=False,seed=27):
    source=Path(vectors).read_bytes(); _,seeds=n.integer_array([seed],bits=64)
    if type(rtl) is not bool: raise ValueError("rtl must be bool")
    data=n.octets(source); out=n.buffer(4<<20)
    size=n.call("tm_exp_conformance",n.C.c_int64,[n.U8,n.SZ,n.C.c_bool,n.C.c_int64,n.U8,n.SZ],data,len(source),rtl,seeds[0],out,len(out))
    if size<0:
        if rtl and -86<=size<=-80:
            from .rtl_compute import RTLSimulationError
            raise RTLSimulationError(f"Native conformance RTL failed ({size})")
        raise ValueError(f"Native conformance failed ({size})")
    return json.loads(bytes(out[:size]))
