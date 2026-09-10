"""ctypes adapters for actual Icarus execution orchestrated by native t27."""
from __future__ import annotations
from pathlib import Path
import shutil
from . import _native as n
from .codecs import get_codec

class RTLSimulationError(RuntimeError): pass
class _Expected(n.C.Structure):
    _fields_=[("result",n.C.c_int64),("error",n.C.c_bool)]
class _Observed(n.C.Structure):
    _fields_=[("index",n.C.c_uint64),("result",n.C.c_int64),("error",n.C.c_bool),("cycles",n.C.c_uint64)]
class _Summary(n.C.Structure):
    _fields_=[(key,n.C.c_uint64) for key in ("cycles","groups","input_stalls","output_stalls","source_bubbles","resets")]+[("result_count",n.SZ)]
class _Witness(n.C.Structure):
    _fields_=[("result",n.C.c_int64),("error",n.C.c_bool)]+[(key,n.C.c_uint64) for key in
        ("cycles","groups","input_stalls","output_stalls","source_bubbles","resets","result_cycle")]+[("weight_count",n.SZ),("encoded_weight_bits",n.C.c_uint64)]
U64=n.C.POINTER(n.C.c_uint64)

def _codec_name(codec):
    if codec=="baseline5": return "baseline2"
    if codec not in ("dense5","baseline2"): raise ValueError("RTL dot supports dense5 or baseline2")
    return codec

def _require_tools():
    paths=tuple(shutil.which(tool) for tool in ("iverilog","vvp"))
    if not all(paths): raise RTLSimulationError("Install Icarus Verilog (iverilog and vvp); no software fallback is available")
    return paths

def _rtl_directory():
    output=n.buffer(4096)
    size=n.call("tm_process_resource",n.C.c_int64,[n.C.c_char_p,n.U8,n.SZ],b"trinity_dot_stream.v",output,len(output))
    if size<0: raise RTLSimulationError("RTL sources unavailable; rebuild or install the complete platform wheel")
    return Path(bytes(output[:size]).decode()).parent

def _status(status):
    if status>=0: return
    if status==-80: raise ValueError("Invalid RTL arguments, lanes, seed or accumulator width")
    if status==-82: raise OverflowError("dot product exceeds signed32 at a group boundary")
    raise RTLSimulationError(f"Native RTL simulation failed ({status}); check Icarus tools and generated resources")

def _packet(code,activations,mask,last):
    _,data=n.integer_array(activations)
    if len(activations)!=5 or type(code) is not int or not 0<=code<=0xffffffff or type(mask) is not int or not 0<=mask<=0xffffffff:
        raise ValueError("Invalid packet representation")
    out=n.C.c_uint64()
    _status(n.call("tm_rtl_packet",n.C.c_int32,[n.C.c_uint32,n.I32,n.C.c_uint32,n.C.c_bool,U64],code,data,mask,last,n.C.byref(out)))
    return out.value

def _packets(weights,activations,codec):
    codec=_codec_name(codec); items,data=n.integer_array(weights); acts,activation_data=n.integer_array(activations)
    if len(items)!=len(acts): raise ValueError("weights and activations must have equal lengths")
    count=n.call("tm_rtl_packet_count",n.C.c_int64,[n.SZ],len(items)); _status(count)
    packets=(n.C.c_uint64*count)(); expected=_Expected()
    size=n.call("tm_rtl_prepare_dot",n.C.c_int64,[n.C.c_int32,n.I32,n.I32,n.SZ,U64,n.SZ,n.C.POINTER(_Expected)],
                get_codec(codec).id,data,activation_data,len(items),packets,count,n.C.byref(expected))
    _status(size); return list(packets[:size])

def _metrics(value,codec,seed,acc_width):
    result={key:getattr(value,key) for key in ("cycles","groups","input_stalls","output_stalls","source_bubbles","resets")}
    return dict(result,codec=codec,evidence="rtl-simulation",simulator="Icarus Verilog",accumulator_bits=acc_width,
                seed=seed,stalls=result["input_stalls"]+result["output_stalls"])

def _run_packets(packets,expected,codec,seed=27,acc_width=32):
    codec=_codec_name(codec); _,seeds=n.integer_array([seed],bits=64)
    if type(acc_width) is not int or not 0<=acc_width<=0xffffffff: raise ValueError("Invalid accumulator width")
    if any(type(value) is not int or not 0<=value<=0xffffffffffffffff for value in packets): raise ValueError("Invalid packet word")
    for result,error in expected:
        n.integer_array([result],bits=64)
        if type(error) is not bool: raise ValueError("Expected error must be bool")
    inputs=(n.C.c_uint64*max(1,len(packets)))(*packets)
    wanted=(_Expected*max(1,len(expected)))(*[_Expected(*item) for item in expected])
    observed=(_Observed*max(1,len(expected)))(); summary=_Summary()
    _require_tools()
    status=n.call("tm_rtl_run_frames",n.C.c_int32,[U64,n.SZ,n.C.POINTER(_Expected),n.SZ,n.C.c_int32,n.C.c_int64,n.C.c_uint32,n.C.POINTER(_Observed),n.SZ,n.C.POINTER(_Summary)],
                  inputs,len(packets),wanted,len(expected),get_codec(codec).id,seeds[0],acc_width,observed,len(expected),n.C.byref(summary))
    _status(status)
    return dict(_metrics(summary,codec,seed,acc_width),results=[dict(index=r.index,result=r.result,error=int(r.error),cycles=r.cycles) for r in observed[:summary.result_count]])

def run_rtl_dot(weights,activations,codec="dense5",seed=27):
    codec=_codec_name(codec); items,data=n.integer_array(weights); acts,activation_data=n.integer_array(activations)
    if len(items)!=len(acts): raise ValueError("weights and activations must have equal lengths")
    _,seeds=n.integer_array([seed],bits=64); witness=_Witness(); _require_tools()
    status=n.call("tm_rtl_run_dot",n.C.c_int32,[n.I32,n.I32,n.SZ,n.C.c_int32,n.C.c_int64,n.C.POINTER(_Witness)],
                  data,activation_data,len(items),get_codec(codec).id,seeds[0],n.C.byref(witness))
    _status(status)
    return dict(_metrics(witness,codec,seed,32),**{key:getattr(witness,key) for key in
                ("result","error","weight_count","result_cycle","encoded_weight_bits")})
