#!/usr/bin/env python3
"""A deliberately wrong decoder for the self-test of ternary-check/action.yml.

It forwards every call of the CLI contract to the reference decoder
(`python3 -m trinity_memory ternary-check`), except that it reads PQ2_0 bytes
(PrismML, group 128: 34-byte blocks) as ggml-org Q2_0 (group 64: 18-byte
blocks) and, like a lenient loader, drops the bytes that do not fill a last
18-byte block. That is the layout confusion of conformance vector
group_128_bytes_read_as_group_64, a synthetic case of this repository (the
published Q2_0 file holds valid group-64 blocks). The Action must report
mismatches for it and fail. This script decodes nothing itself: it only rewrites the
call and cuts the input file.
"""
import os
import sys

arguments = sys.argv[1:]
if len(arguments) >= 6 and arguments[0] == "decode" and arguments[1] == "PQ2_0":
    with open(arguments[3], "rb") as source:
        data = source.read()
    blocks = len(data) // 18
    with open("input-as-q2_0.bin", "wb") as cut:
        cut.write(data[:blocks * 18])
    arguments[1:4] = ["Q2_0", str(blocks * 64), os.path.abspath("input-as-q2_0.bin")]
os.execvp(sys.executable, [sys.executable, "-m", "trinity_memory", "ternary-check", *arguments])
