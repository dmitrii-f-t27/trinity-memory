#!/usr/bin/env python3
"""Copy the t27-generated modules the kit uses into kits/processorci/rtl/generated/, or check the copies.

The copies let ProcessorCI (or anyone) build the kit without the t27 compiler. They must stay byte-identical
to what scripts/generate_rtl.py writes into build/t27/rtl/ with the pinned compiler (native/compiler.lock);
CI regenerates and runs this with --check.
"""
import argparse, hashlib, json, shutil, sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[3]
KIT = ROOT / "kits/processorci"
MODULES = ["fp8_e4m3_decode", "fp8_e4m3_encode", "dense5_decoder", "baseline5_decoder", "sparse41_decoder"]
sha = lambda p: hashlib.sha256(p.read_bytes()).hexdigest()

def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--generated-dir", type=Path, default=ROOT / "build/t27/rtl")
    a = ap.parse_args()
    out = KIT / "rtl/generated"
    prov = {"compiler_revision": (ROOT / "native/compiler.lock").read_text().strip(),
            "generator": "scripts/generate_rtl.py (t27c gen-verilog)", "modules": {}}
    bad = []
    for m in MODULES:
        src, dst = a.generated_dir / f"{m}.v", out / f"{m}.v"
        if not src.is_file():
            print(f"missing {src}; run scripts/generate_rtl.py first", file=sys.stderr); return 2
        prov["modules"][m] = {"source": f"t27/rtl/{m}.t27", "source_sha256": sha(ROOT / f"t27/rtl/{m}.t27"),
                              "generated_sha256": sha(src)}
        if a.check:
            if not dst.is_file() or dst.read_bytes() != src.read_bytes(): bad.append(m)
        else:
            out.mkdir(parents=True, exist_ok=True); shutil.copyfile(src, dst)
    pj = out / "provenance.json"
    text = json.dumps(prov, indent=2) + "\n"
    if a.check:
        if not pj.is_file() or pj.read_text() != text: bad.append("provenance.json")
        if bad: print("kit copies differ from fresh generation: " + ", ".join(bad), file=sys.stderr); return 1
        print(f"kit copies of {len(MODULES)} generated modules match fresh generation"); return 0
    pj.write_text(text); print(f"copied {len(MODULES)} generated modules and wrote provenance.json"); return 0
if __name__ == "__main__":
    raise SystemExit(main())
