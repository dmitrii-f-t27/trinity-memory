"""Python representations for native t27 Edge demo and report generation."""
import json
from pathlib import Path
from . import _native as n
from .codecs import CodecError,get_codec
LABELS=("rising","falling","alternating")
TEMPLATES=((-1,)*6+(1,)*6,(1,)*6+(-1,)*6,(-1,1)*6)

def signal_model(codec="dense5"):
    out=n.buffer(1<<20)
    size=n.call("tm_exp_signal_model",n.C.c_int64,[n.C.c_int32,n.U8,n.SZ],get_codec(codec).id,out,len(out))
    if size<0: raise CodecError(f"Native signal model failed ({size})")
    return bytes(out[:size])

def fixture_cases():
    out=n.buffer(16384)
    size=n.call("tm_exp_fixtures_json",n.C.c_int64,[n.U8,n.SZ],out,len(out))
    if size<0: raise ValueError("Native fixture export failed")
    return json.loads(bytes(out[:size]))

def classify(client,handle,samples):
    result=client.dot(handle,"signal_templates",samples)
    raw,data=n.integer_array(result["accumulators"],bits=64)
    scales,scale_data=n.real_array(result["scales"])
    scores=(n.C.c_double*max(1,len(raw)))(); best=n.C.c_int64(); tied=n.C.c_int64()
    status=n.call("tm_api_classify",n.C.c_int32,[n.I64,n.SZ,n.F64,n.SZ,n.F64,n.I64,n.I64],
                  data,len(raw),scale_data,len(scales),scores,n.C.byref(best),n.C.byref(tied))
    if status: raise ValueError("Signal model requires three finite scaled output rows")
    label=None if tied.value else n.call("tm_exp_label",n.C.c_char_p,[n.SZ],best.value).decode()
    return dict(label=label,ambiguous=bool(tied.value),accumulators=raw,scores=list(scores[:len(raw)]),backend=result["backend"])

def run_edge_demo(*,rtl=False,seed=27):
    if type(rtl) is not bool: raise ValueError("rtl must be bool")
    _,seeds=n.integer_array([seed],bits=64); out=n.buffer(4<<20)
    size=n.call("tm_exp_edge",n.C.c_int64,[n.C.c_bool,n.C.c_int64,n.U8,n.SZ],rtl,seeds[0],out,len(out))
    if size<0:
        if rtl and -86<=size<=-80:
            from .rtl_compute import RTLSimulationError
            raise RTLSimulationError(f"Native Edge RTL failed ({size})")
        raise ValueError(f"Native Edge demo failed ({size})")
    return json.loads(bytes(out[:size]))

def render_edge_report(report,path):
    source=n.json_bytes(report); data=n.octets(source); out=n.buffer(max(4<<20,len(source)*8))
    size=n.call("tm_report_edge_html",n.C.c_int64,[n.U8,n.SZ,n.U8,n.SZ],data,len(source),out,len(out))
    if size<0: raise ValueError(f"Native Edge report failed ({size})")
    path=Path(path); path.parent.mkdir(parents=True,exist_ok=True); path.write_bytes(bytes(out[:size]))
