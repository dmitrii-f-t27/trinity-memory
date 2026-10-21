#!/usr/bin/env python3
"""Fail-closed timing gate on a nextpnr-xilinx log.

  check_timing.py nextpnr.log --expect clk200=200 --expect clk25=25 --json OUT

Each --expect NAME=MHZ names a clock domain constrained by create_clock in the XDC. The gate
requires, in the last (post-route) report of the log, an entry for every expected name whose
constraint equals the expected frequency, verdict PASS and an estimated Fmax at or above it.
A missing domain, an unconstrained one (constraint different from the expected one, e.g. the
global --freq default), a FAIL, or any other reported domain that fails is an error.
Line format: Max frequency for clock 'clk25': 61.2 MHz (PASS at 25.00 MHz)
"""
import argparse
import json
import re
import sys

ap = argparse.ArgumentParser()
ap.add_argument("log")
ap.add_argument("--expect", action="append", required=True, metavar="NAME=MHZ")
ap.add_argument("--json")
a = ap.parse_args()
text = open(a.log, errors="replace").read()
found = {}
for m in re.finditer(r"Max frequency for clock\s+'([^']+)':\s*([0-9.]+) MHz \((PASS|FAIL) at ([0-9.]+) MHz\)", text):
    found[m.group(1)] = {"fmax_mhz": float(m.group(2)), "verdict": m.group(3), "constraint_mhz": float(m.group(4))}
errors = []
expected = {}
for e in a.expect:
    n, f = e.split("=")
    expected[n] = float(f)
for name, mhz in expected.items():
    r = found.get(name)
    if r is None:
        errors.append("no timing report for expected clock domain %r (reported: %s)" % (name, sorted(found)))
        continue
    r["required_mhz"] = mhz
    if abs(r["constraint_mhz"] - mhz) > 0.01:
        errors.append("clock %s is constrained at %.2f MHz, expected %.2f MHz (unconstrained or wrong constraint)" % (name, r["constraint_mhz"], mhz))
    if r["verdict"] != "PASS" or r["fmax_mhz"] < mhz:
        errors.append("clock %s: Fmax %.2f MHz, %s at %.2f MHz, required %.2f MHz" % (name, r["fmax_mhz"], r["verdict"], r["constraint_mhz"], mhz))
for name, r in found.items():
    if name not in expected:
        r["unexpected_domain"] = True
        if r["verdict"] != "PASS":
            errors.append("additional clock domain %s fails: %s" % (name, r))
if re.search(r"Failed to find a route|^ERROR:", text, re.M):
    errors.append("nextpnr reported a routing failure or ERROR")
print(json.dumps(found, indent=1))
for e in errors:
    print("TIMING ERROR:", e)
if a.json:
    open(a.json, "w").write(json.dumps({"clocks": found, "errors": errors}, indent=1))
sys.exit(1 if errors else 0)
