"""Pinned compiler and actual clocked full attention simulation (S=1 slice)."""
import ctypes
from pathlib import Path
from tools import gf16_wide_build as base
from tools import gf16_attn_vectors as vectors
from tools import attn_reference as ar

ROOT = base.ROOT


def generate(work):
    work = Path(work).resolve()
    pin = (ROOT / "native/compiler.lock").read_text().strip()
    if base.run(["git", "-C", base.COMPILER.parents[2], "rev-parse", "HEAD"]).strip() != pin:
        raise ValueError("compiler pin mismatch")
    source = ROOT / "t27/rtl/gf16_attn.t27"
    data = base.run([base.COMPILER, "gen-verilog", source])
    if data != base.run([base.COMPILER, "gen-verilog", source]):
        raise ValueError("nondeterministic attention RTL")
    work.mkdir(parents=True, exist_ok=True)
    path = work / "gf16_attn.v"
    path.write_text(data)
    rtl = [path]
    helper = work / "attn_helpers.t27"
    helper.write_text(source.read_text().split("var state:")[0])
    c = work / "attn_helpers.c"
    c.write_text(base.run([base.COMPILER, "gen-c", helper]))
    lib = work / "attn_helpers.so"
    base.run(["cc", "-std=c11", "-O2", "-shared", "-fPIC", c, "-o", lib])
    return rtl, ctypes.CDLL(str(lib))


def compile_sim(work, rtl, shape, simulator="iverilog", mem_base=2, out_base=1, test_params=None):
    work = Path(work).resolve(); work.mkdir(parents=True, exist_ok=True)
    hidden, kv, heads, head_dim = shape
    params = {"H": hidden, "KV": kv, "HEADS": heads, "HD": head_dim,
              "MEM_BASE": mem_base, "OUT_BASE": out_base}
    if test_params:
        params.update(test_params)
    sources = [*rtl, ROOT / "tests/tb_gf16_attn.v"]
    if simulator == "verilator":
        base.run(["verilator", "--binary", "--timing", "--top-module", "tb_gf16_attn",
                  "-Wno-fatal", "-j", "4", "--Mdir", work / "obj_dir",
                  *[f"-G{k}={v}" for k, v in params.items()], *sources], timeout=240)
        command = [work / "obj_dir/Vtb_gf16_attn"]
    else:
        exe = work / "sim.vvp"
        base.run(["iverilog", "-g2012", "-s", "tb_gf16_attn", "-o", exe,
                  *[f"-Ptb_gf16_attn.{k}={v}" for k, v in params.items()], *sources], timeout=120)
        command = ["vvp", exe]
    return command


def run_sim(work, command, inp, stages):
    work = Path(work).resolve(); work.mkdir(parents=True, exist_ok=True)
    capture = work / "capture.txt"
    log = base.run([*command, "+input=" + str(inp), "+output=" + str(capture)], timeout=1800)
    (work / "rtl.log").write_text(log)
    return vectors.validate(capture.read_bytes(), stages)


def simulate(work, model, xs, simulator="iverilog", mem_base=2, out_base=1):
    """One-position attention run: stages come from the oracle datapath."""
    work = Path(work).resolve(); work.mkdir(parents=True, exist_ok=True)
    dims = model["dims"]
    tables_f64, tables_q16 = ar.rope_tables(len(xs), ar.ROPE_THETA, dims["head_dim"])
    stages, sat = ar.fpga_q16(model, [[t << ar.Q for t in x] for x in xs], tables_q16)
    if any(sat.values()):
        raise ValueError(f"attention datapath saturations: {sat}")
    inp = vectors.write_inputs(work, model, [[t << ar.Q for t in x] for x in xs])
    shape = (dims["hidden"], dims["kv_dim"], dims["heads"], dims["head_dim"])
    command = compile_sim(work, generate(work)[0], shape, simulator, mem_base, out_base)
    return run_sim(work, command, inp, stages)
