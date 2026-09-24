"""DDR3 build of the AX7203 (issue #60): pins, pinned UberDDR3 files, PLL check, status line.

Covers what can be checked without the board and without the toolchain image:
- fpga/ax7203/ddr3/uberddr3.lock names a commit, a license and sha256 per file, and
  no UberDDR3 file is tracked in this repository (it is GPL-3.0-or-later);
- tools/fetch-uberddr3.sh keeps a file only when its sha256 matches (run offline
  against a file:// mirror), and --verify reports an edited file without replacing it;
- tools/git-revision.sh never reports the HEAD of an enclosing repository for a plain
  directory, and reads the commit of an assembled database;
- the report keeps a reproducible identity next to the whole-file .bit sha256 (the header
  holds the build time), refuses a netlist whose BUILD_ID is not the report's commit or
  fetched files that do not match the lock, copies the XDC, and counts the LUT levels into
  the PHY primitives and the IOLOGIC clocks fed from the fabric;
- make ddr3-flash never builds and flashes only a bitstream with the expected sha256, or,
  against a report, one whose whole file or whose part from the sync word on matches it;
- the DDR3 XDC agrees pin for pin with the LiteX AX7203 platform (litex-boards 9f84c87,
  alinx_ax7203.py L102-L125) and with the ports of our top, with the I/O standards,
  termination and slew of every pin, and reuses the board's verified reset and LED pins;
- tools/fpga-build-report.py decodes a PLLE2 from FASM and checks its lock and filter
  tables against Vivado's values for the programmed CLKFBOUT_MULT;
- with T27_ROOT and Icarus: t27/rtl/fpga_ddr3_status.t27 with the line emitter and the
  UART transmitter reports calibration states, returns to IDLE and completion.
"""
from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DDR3 = ROOT / "fpga/ax7203/ddr3"
COMPILER = Path(os.environ["T27_ROOT"]) / "target/release/t27c" if os.environ.get("T27_ROOT") else None

# LiteX AX7203 platform (litex-hub/litex-boards 9f84c87, platforms/alinx_ax7203.py L102-L125).
LITEX = {
    "a": "AA4 AB2 AA5 AB5 AB1 U3 W1 T1 V2 U2 Y1 W2 Y2 U1 V3",
    "ba": "AA3 Y3 Y4",
    "dm": "D2 G2 M2 M5",
    "dq": "C2 G1 A1 F3 B2 F1 B1 E2 H3 G3 H2 H5 J1 J5 K1 H4 L4 M3 L3 J6 K3 K6 J4 L5 P1 N4 R1 N2 M6 N5 P6 P2",
    "dqs_p": "E1 K2 M1 P5",
    "dqs_n": "D1 J2 L1 P4",
}
LITEX_SINGLE = {"ddr3_ras_n": "V4", "ddr3_cas_n": "W4", "ddr3_we_n": "AA1", "ddr3_ck_p": "R3", "ddr3_ck_n": "R2",
                "ddr3_cke": "T5", "ddr3_odt": "U5", "ddr3_reset_n": "W6", "ddr3_cs_n": "AB3"}


def expected_pins(lanes: int) -> dict[str, str]:
    pins = dict(LITEX_SINGLE)
    for i, p in enumerate(LITEX["a"].split()):
        pins[f"ddr3_addr[{i}]"] = p
    for i, p in enumerate(LITEX["ba"].split()):
        pins[f"ddr3_ba[{i}]"] = p
    for i, p in enumerate(LITEX["dq"].split()[:8 * lanes]):
        pins[f"ddr3_dq[{i}]"] = p
    for name in ("dm", "dqs_p", "dqs_n"):
        for i, p in enumerate(LITEX[name].split()[:lanes]):
            pins[f"ddr3_{name}[{i}]"] = p
    return pins


def xdc_properties(*paths: Path) -> dict[str, dict[str, str]]:
    props: dict[str, dict[str, str]] = {}
    for path in paths:
        for m in re.finditer(r"^set_property\s+(\S+)\s+(\S+)\s+\[get_ports\s+(\S+)\]\s*$", path.read_text(), re.M):
            slot = props.setdefault(m.group(3), {})
            if m.group(1) in slot:
                raise AssertionError(f"{m.group(3)}: {m.group(1)} set twice")
            slot[m.group(1)] = m.group(2)
    return props


def load_report_module():
    spec = importlib.util.spec_from_file_location("fpga_build_report", ROOT / "tools/fpga-build-report.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def sha256_hex(data: bytes) -> str:
    import hashlib
    return hashlib.sha256(data).hexdigest()


class UberDdr3Lock(unittest.TestCase):
    def test_lock_pins_commit_license_and_hashes(self):
        lock = load_report_module().parse_lock(DDR3 / "uberddr3.lock")
        self.assertEqual(lock["repository"], "https://github.com/AngeloJacobo/UberDDR3")
        self.assertRegex(lock["commit"], r"^[0-9a-f]{40}$")
        self.assertEqual(lock["license"], "GPL-3.0-or-later")
        self.assertEqual({"rtl/ddr3_top.v", "rtl/ddr3_controller.v", "rtl/ddr3_phy.v"} - set(lock["files"]), set())
        for path, digest in lock["files"].items():
            self.assertRegex(digest, r"^[0-9a-f]{64}$", path)
            self.assertNotIn("example_demo", path)

    def test_no_uberddr3_file_is_committed(self):
        if not (ROOT / ".git").exists():
            self.skipTest("not a git checkout")
        tracked = subprocess.run(["git", "-C", str(ROOT), "ls-files"], capture_output=True, text=True, check=True).stdout.split()
        names = {"ddr3_top.v", "ddr3_controller.v", "ddr3_phy.v", "ax7103_ddr3.v", "clk_wiz.v"}
        self.assertEqual([t for t in tracked if Path(t).name in names], [])
        self.assertEqual([t for t in tracked if t.endswith(".bit")], [])

    @unittest.skipUnless(shutil.which("curl"), "curl required")
    def test_fetch_checks_every_hash(self):
        with tempfile.TemporaryDirectory(prefix="trinity-uberddr3-") as tmp:
            tmp = Path(tmp)
            mirror = tmp / "mirror"
            (mirror / "rtl").mkdir(parents=True)
            body = b"module ddr3_top; endmodule\n"
            (mirror / "rtl/ddr3_top.v").write_bytes(body)
            head = f"repository https://github.com/example/UberDDR3\ncommit {'a' * 40}\nlicense GPL-3.0-or-later\n"
            good, bad = tmp / "good.lock", tmp / "bad.lock"
            good.write_text(head + f"file {sha256_hex(body)} rtl/ddr3_top.v\n")
            bad.write_text(head + f"file {'0' * 64} rtl/ddr3_top.v\n")
            env = dict(os.environ, UBERDDR3_BASE_URL=mirror.as_uri())
            out = tmp / "out"
            run = subprocess.run(["sh", str(ROOT / "tools/fetch-uberddr3.sh"), str(bad), str(out)],
                                 capture_output=True, text=True, env=env)
            self.assertNotEqual(run.returncode, 0)
            self.assertIn("sha256 mismatch", run.stderr)
            self.assertFalse((out / "rtl/ddr3_top.v").exists())
            self.assertFalse((out / "SOURCE").exists())
            run = subprocess.run(["sh", str(ROOT / "tools/fetch-uberddr3.sh"), str(good), str(out)],
                                 capture_output=True, text=True, env=env)
            self.assertEqual(run.returncode, 0, run.stderr)
            self.assertEqual((out / "rtl/ddr3_top.v").read_bytes(), body)
            self.assertIn("license GPL-3.0-or-later", (out / "SOURCE").read_text())
            run = subprocess.run(["sh", str(ROOT / "tools/fetch-uberddr3.sh"), "--verify", str(good), str(out)],
                                 capture_output=True, text=True, env=env)
            self.assertEqual(run.returncode, 0, run.stderr)
            # --verify (run before every synthesis) stops on an edited copy and leaves it alone.
            (out / "rtl/ddr3_top.v").write_bytes(b"tampered\n")
            run = subprocess.run(["sh", str(ROOT / "tools/fetch-uberddr3.sh"), "--verify", str(good), str(out)],
                                 capture_output=True, text=True, env=dict(env, UBERDDR3_BASE_URL="file:///nonexistent"))
            self.assertNotEqual(run.returncode, 0)
            self.assertIn("does not match the pinned sha256", run.stderr)
            self.assertEqual((out / "rtl/ddr3_top.v").read_bytes(), b"tampered\n")
            # A fetch replaces the tampered copy with a checked download.
            run = subprocess.run(["sh", str(ROOT / "tools/fetch-uberddr3.sh"), str(good), str(out)],
                                 capture_output=True, text=True, env=env)
            self.assertEqual(run.returncode, 0, run.stderr)
            self.assertEqual((out / "rtl/ddr3_top.v").read_bytes(), body)

    def test_synthesis_checks_the_hashes_and_the_build_id(self):
        makefile = (ROOT / "fpga/ax7203/Makefile").read_text()
        rule = re.search(r"^\$\(DDR3_BUILD\)/\$\(DDR3_TOP\)\.json:(.*)\n((?:\t.*\n)+)", makefile, re.M)
        self.assertIsNotNone(rule)
        self.assertIn("build-id-$(BUILD_ID).stamp", rule.group(1))
        self.assertIn("fetch-uberddr3.sh --verify", rule.group(2).splitlines()[0])


@unittest.skipUnless(shutil.which("git"), "git required")
class GitRevision(unittest.TestCase):
    def run_rev(self, *args):
        return subprocess.run(["sh", str(ROOT / "tools/git-revision.sh"), *map(str, args)],
                              capture_output=True, text=True, check=True).stdout.strip()

    def test_plain_directory_inside_a_repository(self):
        with tempfile.TemporaryDirectory(prefix="trinity-gitrev-") as tmp:
            repo = Path(tmp) / "repo"
            (repo / "build/db").mkdir(parents=True)
            (repo / "build/tool").mkdir(parents=True)
            git = ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.invalid"]
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            (repo / "f").write_text("x\n")
            subprocess.run(git + ["add", "f"], check=True)
            subprocess.run(git + ["commit", "-q", "-m", "x"], check=True)
            head = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True,
                                  check=True).stdout.strip()
            (repo / "build/db/ASSEMBLED-openXC7_prjxray-db-a90f27c1caef.txt").write_text(
                "assembled from files\ncommit a90f27c1caefee5276f47440f4c730b50519a86f\n")
            self.assertEqual(self.run_rev(repo), head)
            self.assertEqual(self.run_rev(repo / "build/db"), "a90f27c1caefee5276f47440f4c730b50519a86f")
            self.assertEqual(self.run_rev(repo / "build/tool"), "unknown")
            self.assertEqual(self.run_rev("--describe", repo / "build/tool"), "unknown")
            self.assertEqual(self.run_rev(Path(tmp) / "missing"), "unknown")


class Ddr3Constraints(unittest.TestCase):
    def setUp(self):
        self.x16 = xdc_properties(DDR3 / "tms_ddr3_ax7203.xdc")
        self.x32 = xdc_properties(DDR3 / "tms_ddr3_ax7203.xdc", DDR3 / "tms_ddr3_x32.xdc")

    def test_pins_equal_the_litex_platform(self):
        for props, lanes in ((self.x16, 2), (self.x32, 4)):
            got = {port: p["PACKAGE_PIN"] for port, p in props.items() if port.startswith("ddr3_")}
            self.assertEqual(got, expected_pins(lanes))
        self.assertEqual(len([p for p in self.x32 if p.startswith("ddr3_")]), 71)

    def test_no_package_pin_is_used_twice(self):
        pins = [p["PACKAGE_PIN"] for p in self.x32.values()]
        self.assertEqual(len(pins), len(set(pins)))

    def test_io_standards_termination_and_slew(self):
        for port, p in self.x32.items():
            if not port.startswith("ddr3_"):
                continue
            want = ("DIFF_SSTL15" if port.startswith(("ddr3_dqs", "ddr3_ck_")) else
                    "LVCMOS15" if port == "ddr3_reset_n" else "SSTL15")
            self.assertEqual(p.get("IOSTANDARD"), want, port)
            self.assertEqual(p.get("SLEW"), "FAST", port)
            if port.startswith(("ddr3_dq[", "ddr3_dqs")):
                self.assertEqual(p.get("IN_TERM"), "UNTUNED_SPLIT_50", port)
            else:
                self.assertNotIn("IN_TERM", p, port)

    def test_board_pins_as_in_the_verified_xdc(self):
        board = xdc_properties(ROOT / "fpga/ax7203/tms_trace_player.xdc")
        for port in ("clk200_p", "clk200_n", "rst_n", "led[0]", "led[1]", "led[2]", "led[3]", "uart_tx"):
            self.assertEqual(self.x16[port], board[port], port)
        self.assertEqual(self.x16["rst_n"], {"PACKAGE_PIN": "T6", "IOSTANDARD": "LVCMOS15"})

    def test_xdc_ports_exist_on_the_top(self):
        top = (DDR3 / "tms_ddr3_ax7203.v").read_text()
        ports = set(re.findall(r"^\s*(?:input|output|inout)\s+wire\s*(?:\[[^\]]+\])?\s*(\w+)", top, re.M))
        for port in self.x32:
            base = port.split("[")[0]
            self.assertIn(base, ports, port)

    def test_makefile_writes_create_clock_for_every_pll_output(self):
        makefile = (ROOT / "fpga/ax7203/Makefile").read_text()
        for net in ("clk_ctrl", "clk_ddr", "clk_ref", "clk_ddr_90"):
            self.assertRegex(makefile, r"create_clock -period \S+ -name %s \[get_nets %s\]" % (net, net))
            self.assertRegex((DDR3 / "tms_ddr3_ax7203.v").read_text(), r"BUFG \w+\s*\(\.I\(\w+\),\s*\.O\(%s\)\)" % net)


FASM_PLL = """\
CMT_TOP_L_UPPER_T_X256Y148.PLLE2_ADV.IN_USE
CMT_TOP_L_UPPER_T_X256Y148.PLLE2_ADV.DIVCLK_DIVCLK_HIGH_TIME[5:0] = 6'b000001
CMT_TOP_L_UPPER_T_X256Y148.PLLE2_ADV.DIVCLK_DIVCLK_LOW_TIME[5:0] = 6'b000001
CMT_TOP_L_UPPER_T_X256Y148.PLLE2_ADV.DIVCLK_DIVCLK_NO_COUNT[0]
CMT_TOP_L_UPPER_T_X256Y148.PLLE2_ADV.CLKFBOUT_CLKOUT1_OUTPUT_ENABLE[0]
CMT_TOP_L_UPPER_T_X256Y148.PLLE2_ADV.CLKFBOUT_CLKOUT1_HIGH_TIME[5:0] = 6'b000010
CMT_TOP_L_UPPER_T_X256Y148.PLLE2_ADV.CLKFBOUT_CLKOUT1_LOW_TIME[5:0] = 6'b000011
CMT_TOP_L_UPPER_T_X256Y148.PLLE2_ADV.CLKOUT3_CLKOUT1_OUTPUT_ENABLE[0]
CMT_TOP_L_UPPER_T_X256Y148.PLLE2_ADV.CLKOUT3_CLKOUT1_HIGH_TIME[5:0] = 6'b000001
CMT_TOP_L_UPPER_T_X256Y148.PLLE2_ADV.CLKOUT3_CLKOUT1_LOW_TIME[5:0] = 6'b000010
CMT_TOP_L_UPPER_T_X256Y148.PLLE2_ADV.CLKOUT3_CLKOUT1_PHASE_MUX[2:0] = 3'b110
CMT_TOP_L_UPPER_T_X256Y148.PLLE2_ADV.FILTREG1_RESERVED[11:0] = 12'b000000001000
CMT_TOP_L_UPPER_T_X256Y148.PLLE2_ADV.LKTABLE[39:0] = 40'b{lk:040b}
CMT_TOP_L_UPPER_T_X256Y148.PLLE2_ADV.TABLE[9:0] = 10'b{table:010b}
HCLK_IOI3_X263Y182.VREF.V_675_MV
"""


class PllTableCheck(unittest.TestCase):
    def setUp(self):
        self.report = load_report_module()

    def test_mult5_with_vivado_tables_passes(self):
        [pll] = self.report.pll_check(FASM_PLL.format(lk=0x73BE8FA401, table=0x1EC))
        self.assertEqual((pll["clkfbout_mult"], pll["divclk_divide"]), (5, 1))
        self.assertEqual(pll["clkout_divide"], {"CLKOUT3": 3})
        self.assertEqual(pll["clkout_phase_mux"], {"CLKOUT3": 6})
        self.assertEqual(pll["result"], "PASS")

    def test_mult8_tables_on_a_mult5_pll_fail(self):
        [pll] = self.report.pll_check(FASM_PLL.format(lk=0xB5BE8FA401, table=0x3B4))
        self.assertEqual(pll["result"], "FAIL")
        self.assertIn("CLKFBOUT_MULT=8", pll["note"])

    def test_declared_clocks(self):
        xdc = "create_clock -period 12.000 -name clk_ctrl [get_nets clk_ctrl]\ncreate_clock -period 5.000 -name clk200 [get_ports clk200_p]\n"
        self.assertEqual(self.report.declared_clocks(xdc), [
            {"name": "clk_ctrl", "period_ns": 12.0, "target": "clk_ctrl"},
            {"name": "clk200", "period_ns": 5.0, "target": "clk200_p"}])

    def test_routed_clocks_skip_the_placer_estimate(self):
        log = ("Info: Max frequency for clock 'clk_ctrl': 70.00 MHz (FAIL at 83.33 MHz)\n"
               "Info: Router2 time 8.00s\n"
               "Info: Max frequency for clock 'clk_ctrl': 90.00 MHz (PASS at 83.33 MHz)\n")
        self.assertEqual(self.report.routed_clocks(log),
                         [{"clock": "clk_ctrl", "fmax_mhz": 90.0, "verdict": "PASS", "target_mhz": 83.33}])


def bit_file(time: str) -> bytes:
    def field(key: bytes, text: str) -> bytes:
        body = text.encode() + b"\0"
        return key + len(body).to_bytes(2, "big") + body
    head = bytes.fromhex("0009") + bytes.fromhex("0ff00ff00ff00ff000") + bytes.fromhex("0001")
    fields = field(b"a", "x.frames;Generator=xc7frames2bit") + field(b"b", "xc7a200tfbg484-2") + \
        field(b"c", "2026/09/24") + field(b"d", time)
    payload = b"\xff" * 16 + bytes.fromhex("000000bb11220044ffffffff") + bytes.fromhex("AA995566") + bytes(range(64))
    return head + fields + b"e" + len(payload).to_bytes(4, "big") + payload


NETLIST = {"modules": {"tms_ddr3_ax7203": {
    "parameter_default_values": {"BUILD_ID": format(0xF07E91CD, "032b")},
    "cells": {
        "ff": {"type": "FDRE", "port_directions": {"C": "input", "D": "input", "Q": "output"},
               "connections": {"C": [1], "D": [9], "Q": [2]}},
        "l1": {"type": "LUT2", "port_directions": {"I0": "input", "I1": "input", "O": "output"},
               "connections": {"I0": [2], "I1": [2], "O": [3]}},
        "c4": {"type": "CARRY4", "port_directions": {"CI": "input", "O": "output"},
               "connections": {"CI": [3], "O": [4]}},
        "l2": {"type": "LUT1", "port_directions": {"I0": "input", "O": "output"},
               "connections": {"I0": [4], "O": [5]}},
        "oser": {"type": "OSERDESE2", "port_directions": {"D1": "input", "D2": "input", "CLK": "input", "OQ": "output"},
                 "connections": {"D1": [5], "D2": [2], "CLK": [1], "OQ": [6]}},
        "iser": {"type": "ISERDESE2", "port_directions": {"Q1": "output", "CLK": "input"},
                 "connections": {"Q1": [7], "CLK": [1]}},
        "l3": {"type": "LUT1", "port_directions": {"I0": "input", "O": "output"},
               "connections": {"I0": [7], "O": [9]}}}}}}

FASM_CLOCKS = """\
RIOI3_TBYTESRC_X105Y193.IOI_OCLK_0.IOI_IMUX31_1
RIOI3_TBYTESRC_X105Y193.IOI_OCLKM_0.IOI_IMUX31_1
RIOI3_X105Y195.IOI_OCLK_0.IOI_LEAF_GCLK2
RIOI3_TBYTESRC_X105Y143.IOI_OLOGIC0_D1.IOI_IMUX34_1
RIOI3_TBYTESRC_X105Y143.OLOGIC_Y0.OMUX.D1
"""


class BuildReportIdentity(unittest.TestCase):
    def setUp(self):
        self.report = load_report_module()

    def test_bitstream_identity_ignores_the_header_time(self):
        with tempfile.TemporaryDirectory(prefix="trinity-bit-") as tmp:
            a, b = Path(tmp) / "a.bit", Path(tmp) / "b.bit"
            a.write_bytes(bit_file("00:53:18"))
            b.write_bytes(bit_file("01:15:12"))
            ia, ib = self.report.bitstream_identity(a), self.report.bitstream_identity(b)
        self.assertNotEqual(ia["sha256"], ib["sha256"])
        self.assertEqual(ia["sha256_from_sync"], ib["sha256_from_sync"])
        self.assertEqual(ia["header"]["d"], "00:53:18")
        self.assertEqual(ia["header"]["b"], "xc7a200tfbg484-2")

    def test_phy_path_levels(self):
        levels = self.report.phy_path_levels(NETLIST["modules"]["tms_ddr3_ax7203"])
        # FF -> LUT2 -> CARRY4 (not a level) -> LUT1 -> OSERDESE2.D1; FF -> D2 directly.
        self.assertEqual(levels["into_phy_max_lut_levels"], {"OSERDESE2.D1": 2, "OSERDESE2.D2": 0})
        self.assertEqual(levels["out_of_phy_max_lut_levels"], {"FDRE.D": 1})
        self.assertEqual(self.report.netlist_build_id(NETLIST["modules"]["tms_ddr3_ax7203"]), "f07e91cd")

    def test_fabric_clock_pips(self):
        record = self.report.fabric_clocks(FASM_CLOCKS)
        self.assertEqual(record["iologic_clock_from_fabric"], [
            "RIOI3_TBYTESRC_X105Y193.IOI_OCLKM_0.IOI_IMUX31_1", "RIOI3_TBYTESRC_X105Y193.IOI_OCLK_0.IOI_IMUX31_1"])
        self.assertEqual(record["ologic_d1_route_through"], ["RIOI3_TBYTESRC_X105Y143.OLOGIC_Y0"])

    def run_report(self, tmp: Path, commit: str, tamper: bool = False):
        art, uber = tmp / "art", tmp / "uber"
        (uber / "rtl").mkdir(parents=True, exist_ok=True)
        art.mkdir(exist_ok=True)
        body = b"module ddr3_top; endmodule\n"
        (uber / "rtl/ddr3_top.v").write_bytes(b"edited\n" if tamper else body)
        lock = tmp / "u.lock"
        lock.write_text(f"repository https://github.com/example/UberDDR3\ncommit {'a' * 40}\nlicense GPL-3.0-or-later\n"
                        f"file {sha256_hex(body)} rtl/ddr3_top.v\n")
        (art / "tms_ddr3_ax7203.json").write_text(json.dumps(NETLIST))
        (art / "tms_ddr3_ax7203.xdc").write_text("create_clock -period 12.000 -name clk_ctrl [get_nets clk_ctrl]\n")
        (art / "tms_ddr3_ax7203.bit").write_bytes(bit_file("00:00:00"))
        (art / "tms_ddr3_ax7203.frames").write_text("0x00000000 0x0\n")
        (art / "tms_ddr3_ax7203.fasm").write_text(FASM_CLOCKS)
        out = tmp / "report"
        run = subprocess.run([sys.executable, str(ROOT / "tools/fpga-build-report.py"), "--artifact-dir", str(art),
                              "--top", "tms_ddr3_ax7203", "--commit", commit, "--output", str(out), "--ddr3",
                              "--uberddr3-dir", str(uber), "--lock", str(lock)], capture_output=True, text=True)
        return run, out

    def test_report_checks_build_id_and_files_and_copies_the_xdc(self):
        with tempfile.TemporaryDirectory(prefix="trinity-report-") as tmp:
            tmp = Path(tmp)
            run, out = self.run_report(tmp, "f07e91cd")
            self.assertEqual(run.returncode, 0, run.stderr)
            record = json.loads((out / "build.json").read_text())
            self.assertTrue((out / "tms_ddr3_ax7203.xdc").is_file())
            self.assertIn("tms_ddr3_ax7203.xdc", record["files"])
            self.assertEqual(record["ddr3"]["netlist_build_id"], "f07e91cd")
            identity = record["reproducible_identity"]
            self.assertEqual(identity["bitstream_sha256_from_sync"], record["bitstream"]["sha256_from_sync"])
            self.assertEqual(identity["frames_sha256"], record["frames"]["sha256"])
            self.assertEqual(identity["fasm_sha256"], record["fasm"]["sha256"])
            run, out = self.run_report(tmp, "5b9574e3")
            self.assertEqual(run.returncode, 1)
            self.assertIn("BUILD_ID f07e91cd", run.stderr)
            run, out = self.run_report(tmp, "f07e91cd", tamper=True)
            self.assertEqual(run.returncode, 1)
            self.assertIn("rtl/ddr3_top.v", run.stderr)
            self.assertFalse(json.loads((out / "build.json").read_text())["ddr3"]["uberddr3"]["files"]["rtl/ddr3_top.v"]["verified"])


@unittest.skipUnless(shutil.which("make"), "make required")
class Ddr3Flash(unittest.TestCase):
    def flash(self, build: Path, *extra: str):
        return subprocess.run(["make", "-s", "-C", str(ROOT / "fpga/ax7203"), "ddr3-flash", f"DDR3_BUILD={build}",
                               "OPENFPGALOADER=echo LOADER", *extra], capture_output=True, text=True)

    def test_flash_never_builds_and_checks_the_hash(self):
        with tempfile.TemporaryDirectory(prefix="trinity-flash-") as tmp:
            build = Path(tmp)
            run = self.flash(build, "DDR3_EXPECT_SHA256=" + "0" * 64)
            self.assertNotEqual(run.returncode, 0)
            self.assertIn("never builds", run.stdout + run.stderr)
            bit = build / "tms_ddr3_ax7203.bit"
            bit.write_bytes(bit_file("00:00:00"))
            run = self.flash(build)
            self.assertNotEqual(run.returncode, 0)
            self.assertIn("DDR3_EXPECT_SHA256", run.stdout + run.stderr)
            run = self.flash(build, "DDR3_EXPECT_SHA256=" + "0" * 64)
            self.assertNotEqual(run.returncode, 0)
            self.assertNotIn("LOADER", run.stdout)
            want = sha256_hex(bit.read_bytes())
            run = self.flash(build, "DDR3_EXPECT_SHA256=" + want)
            self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
            self.assertIn(f"LOADER -c digilent_hs2 {bit}", run.stdout)
            report = build / "build.json"
            report.write_text(json.dumps({"bitstream": {"sha256": want}}))
            run = self.flash(build, f"DDR3_FLASH_REPORT={report}")
            self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
            self.assertIn("LOADER", run.stdout)
            self.assertIn("(whole file)", run.stdout)

    def test_flash_accepts_a_rebuild_by_its_sync_identity(self):
        # A rebuild differs from the report's .bit only in the header time: the
        # whole-file sha256 differs, the sha256 from the sync word on does not.
        with tempfile.TemporaryDirectory(prefix="trinity-flash-") as tmp:
            build = Path(tmp)
            reported = bit_file("00:53:18")
            sync = reported.find(bytes.fromhex("AA995566"))
            report = build / "build.json"
            report.write_text(json.dumps({"bitstream": {
                "sha256": sha256_hex(reported), "sha256_from_sync": sha256_hex(reported[sync:])}}))
            bit = build / "tms_ddr3_ax7203.bit"
            bit.write_bytes(bit_file("09:14:07"))
            self.assertNotEqual(sha256_hex(bit.read_bytes()), sha256_hex(reported))
            run = self.flash(build, f"DDR3_FLASH_REPORT={report}")
            self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
            self.assertIn("from the sync word", run.stdout)
            self.assertIn(f"LOADER -c digilent_hs2 {bit}", run.stdout)
            # The whole-file form stays strict.
            run = self.flash(build, "DDR3_EXPECT_SHA256=" + sha256_hex(reported))
            self.assertNotEqual(run.returncode, 0)
            self.assertNotIn("LOADER", run.stdout)
            # Another payload matches neither.
            bit.write_bytes(bit_file("09:14:07")[:-1] + b"\x00")
            run = self.flash(build, f"DDR3_FLASH_REPORT={report}")
            self.assertNotEqual(run.returncode, 0)
            self.assertNotIn("LOADER", run.stdout)
            self.assertIn("nothing was flashed", run.stdout)

    def test_ddr3_all_refuses_a_single_width(self):
        run = subprocess.run(["make", "-n", "-C", str(ROOT / "fpga/ax7203"), "ddr3-all", "DDR3_WIDTH=16"],
                             capture_output=True, text=True)
        self.assertNotEqual(run.returncode, 0)
        self.assertIn("ddr3-all builds x16 and x32", run.stderr)


TESTBENCH = r"""
`timescale 1ns/1ps
`default_nettype none
module tb_ddr3_status;
    reg clk = 0, rst_n = 0;
    always #5 clk = !clk;
    reg  [31:0] state = 0;
    reg         calib = 0;
    wire line_go, line_idle, tx_start, tx_busy, recalibrated;
    wire [31:0] line_tag, line_a, tx_byte;
    wire [63:0] line_b;
    TrinityFpgaDdr3StatusT27 status (
        .clk(clk), .rst_n(rst_n), .en(1'b1), .ready(),
        .state(state), .calib(calib), .line_idle(line_idle), .build_id(32'hb74926ed),
        .lanes(32'd2), .period_ps(32'd3000), .period(32'd20000),
        .line_go(line_go), .line_tag(line_tag), .line_a(line_a), .line_b(line_b), .recalibrated(recalibrated));
    TrinityFpgaLineEmitterT27 emitter (
        .clk(clk), .rst_n(rst_n), .en(1'b1), .ready(),
        .go(line_go), .tag(line_tag), .a(line_a), .b(line_b), .tx_busy(tx_busy),
        .tx_start(tx_start), .tx_byte(tx_byte), .idle(line_idle));
    TrinityFpgaUartTxT27 uart (
        .clk(clk), .rst_n(rst_n), .en(1'b1), .ready(),
        .start(tx_start), .data(tx_byte), .baud_div(32'd2), .busy(tx_busy), .tx());
    integer f;
    always @(posedge clk) if (tx_start) $fwrite(f, "%c", tx_byte[7:0]);
    task hold(input [31:0] s, input c, input integer n);
        begin state = s; calib = c; repeat (n) @(posedge clk); end
    endtask
    initial begin
        f = $fopen("lines.txt", "w");
        repeat (3) @(posedge clk); rst_n = 1;
        hold(0, 0, 2000); hold(1, 0, 2000); hold(5, 0, 2000); hold(22, 0, 2000);
        hold(0, 0, 2000);                  // a wrong read in the self-test: back to IDLE
        hold(3, 0, 2000); hold(23, 1, 30000); // DONE_CALIBRATE, then one periodic line
        $fclose(f);
        $display("TB_DONE recalibrated=%0d", recalibrated);
        $finish;
    end
endmodule
"""


@unittest.skipUnless(COMPILER and COMPILER.is_file() and shutil.which("iverilog") and shutil.which("vvp"),
                     "T27_ROOT with a built t27c and Icarus required")
class Ddr3StatusLine(unittest.TestCase):
    def test_status_lines(self):
        with tempfile.TemporaryDirectory(prefix="trinity-ddr3-status-") as tmp:
            tmp = Path(tmp)
            sources = []
            for core in ("fpga_ddr3_status", "fpga_line_emitter", "fpga_uart_tx"):
                verilog = subprocess.run([str(COMPILER), "gen-verilog", str(ROOT / f"t27/rtl/{core}.t27")],
                                         capture_output=True, text=True, check=True).stdout
                (tmp / f"{core}.v").write_text(verilog)
                sources.append(str(tmp / f"{core}.v"))
            (tmp / "tb.v").write_text(TESTBENCH)
            subprocess.run(["iverilog", "-g2012", "-o", str(tmp / "tb.vvp"), "-s", "tb_ddr3_status", *sources, str(tmp / "tb.v")],
                           check=True, capture_output=True, text=True)
            run = subprocess.run(["vvp", str(tmp / "tb.vvp")], cwd=tmp, capture_output=True, text=True, check=True)
            self.assertIn("TB_DONE recalibrated=1", run.stdout)
            lines = (tmp / "lines.txt").read_text().splitlines()
        self.assertEqual(lines[0], "Hb74926ed0102000bb8")
        status = []
        for line in lines[1:]:
            self.assertRegex(line, r"^S[0-9a-f]{18}$")
            a = int(line[1:9], 16)
            status.append(((a >> 24) & 1, (a >> 16) & 31, (a >> 8) & 31, a & 255, int(line[9:], 16)))
        # Every state held long enough is reported in order, with the running maximum and the recalibration count.
        self.assertEqual([s[:4] for s in status[:7]],
                         [(0, 0, 0, 0), (0, 1, 1, 0), (0, 5, 5, 0), (0, 22, 22, 0), (0, 0, 22, 1), (0, 3, 22, 1),
                          (1, 23, 23, 1)])
        # Then periodic lines with the same status while nothing changes; the clock count grows.
        self.assertGreaterEqual(len(status), 8)
        self.assertEqual(status[7][:4], (1, 23, 23, 1))
        self.assertEqual([s[4] for s in status], sorted(s[4] for s in status))


if __name__ == "__main__":
    unittest.main()
