#!/usr/bin/env python3
"""Fail-closed checks of the firmware ELF against the soft CPU (PicoRV32, RV32I, no extensions).

  check_elf.py ELF --objdump P --readobj P --nm P [--ram-bytes 32768] [--json OUT]

1. ISA/ABI: the ELF's RISC-V arch attribute is rv32i only (no m, a, c, f, d, zicsr) and the
   ELF flags carry no RVC/float ABI bit.
2. Every instruction in every executable section, disassembled without aliases, is in the
   RV32I base set that trios_soc_core.v's PicoRV32 configuration implements
   (ENABLE_MUL/DIV=0, COMPRESSED_ISA=0, ENABLE_COUNTERS=0, ENABLE_IRQ=0: no CSR, no ecall/ebreak).
3. Memory map: _start is at the reset vector 0, _stack_top is the end of RAM, the image
   plus bss leaves the reserved stack.
"""
import argparse
import json
import re
import subprocess
import sys
from collections import Counter

RV32I = set("""lui auipc jal jalr beq bne blt bge bltu bgeu lb lh lw lbu lhu sb sh sw addi slti sltiu
xori ori andi slli srli srai add sub sll slt sltu xor srl sra or and fence""".split())


def run(cmd):
    return subprocess.run(cmd, check=True, capture_output=True, text=True).stdout


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("elf")
    ap.add_argument("--objdump", required=True)
    ap.add_argument("--readobj", required=True)
    ap.add_argument("--nm", required=True)
    ap.add_argument("--ram-bytes", type=int, default=32768)
    ap.add_argument("--stack-reserve", type=int, default=0x1800)
    ap.add_argument("--json")
    a = ap.parse_args()
    errors = []

    attrs = run([a.readobj, "--arch-specific", a.elf])
    m = re.search(r'TagName:\s*arch\s*\n\s*Value:\s*(\S+)', attrs) or re.search(r'Value:\s*(rv32\S+)', attrs)
    arch = m.group(1) if m else None
    if not arch or not re.fullmatch(r"rv32i[0-9p]*", arch):
        errors.append("arch attribute is %r, expected rv32i with no extensions" % arch)
    hdr = run([a.readobj, "--file-headers", a.elf])
    flags = re.findall(r"EF_RISCV_\w+", hdr)
    if flags:
        errors.append("ELF flags %s: the CPU is full RV32I (32 registers), the ABI is soft-float ilp32 without RVC/RVE" % flags)
    if "Machine: EM_RISCV" not in hdr or "Class: 32-bit" not in hdr:
        errors.append("not a 32-bit RISC-V ELF")

    dis = run([a.objdump, "-d", "--no-show-raw-insn", "-M", "no-aliases", a.elf])
    hist, bad = Counter(), []
    for line in dis.splitlines():
        mm = re.match(r"\s*[0-9a-f]+:\s+(\S+)", line)
        if mm:
            op = mm.group(1)
            hist[op] += 1
            if op not in RV32I:
                bad.append(line.strip())
    if not hist:
        errors.append("no instructions found")
    if bad:
        errors.append("%d instructions outside RV32I, e.g. %s" % (len(bad), bad[:5]))

    syms = {}
    for line in run([a.nm, a.elf]).splitlines():
        parts = line.split()
        if len(parts) == 3:
            syms[parts[2]] = int(parts[0], 16)
    for need in ("_start", "_stack_top", "_sbss", "_ebss", "trios_main"):
        if need not in syms:
            errors.append("symbol %s missing" % need)
    if not errors or all(k in syms for k in ("_start", "_stack_top", "_ebss")):
        if syms.get("_start") != 0:
            errors.append("_start is %#x, reset vector is 0" % syms.get("_start", -1))
        if syms.get("_stack_top") != a.ram_bytes:
            errors.append("_stack_top %#x != RAM end %#x" % (syms.get("_stack_top", -1), a.ram_bytes))
        if syms.get("_ebss", 1 << 40) + a.stack_reserve > a.ram_bytes:
            errors.append("image+bss ends at %#x, less than the %#x stack reserve" % (syms.get("_ebss"), a.stack_reserve))

    result = {"arch": arch, "elf_flags": flags, "instructions": sum(hist.values()),
              "distinct_mnemonics": dict(sorted(hist.items())), "symbols": {k: hex(v) for k, v in syms.items() if k in ("_start", "_stack_top", "_sbss", "_ebss", "trios_main")},
              "errors": errors}
    if a.json:
        open(a.json, "w").write(json.dumps(result, indent=1))
    print("arch", arch, "| instructions", sum(hist.values()), "| mnemonics", len(hist), "| symbols", result["symbols"])
    for e in errors:
        print("ERROR:", e)
    sys.exit(1 if errors else 0)


if __name__ == "__main__":
    main()
