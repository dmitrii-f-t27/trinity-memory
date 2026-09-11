#!/usr/bin/env python3
"""Trinity Conformance Lab: one reproducible report over every consumer.

Sections, as stated in specs/memory/conformance.t27:
  fixture    the native conformance experiment on the lab fixture (loopback
             HTTP transfer, exact dot, RTL replay, six single-bit corruptions)
  codecs     memory_types.json through the Python adapters and the WASM codec
  containers memory_tensorpack.json through the Python adapters and the CLI
  bridge     memory_bridge.json over TCP against the native server, plus the
             interruption checks of the lab itself
  stream     memory_stream_compute.json cycle traces replayed in Icarus
  frames     memory_stream_compute.json frames through the native RTL runner
The report carries evidence labels, counts, source hashes and tool versions.
Wall-clock timings live under "timing"; with --repeat, consecutive runs must
be identical after that key is removed. Nothing is simulated in software when
a simulator is missing: the affected sections are reported as skipped and the
report does not pass.
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import platform
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from http.client import HTTPConnection
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import spec_bridge_replay  # noqa: E402
import spec_stream_replay  # noqa: E402
from trinity_memory import CODECS, CodecError, decode_file, encode_file, pack, unpack  # noqa: E402
from trinity_memory.bridge import BridgeClient, BridgeError, BridgeServer  # noqa: E402
from trinity_memory.conformance import run_conformance  # noqa: E402
from trinity_memory.rtl_compute import _packets, _run_packets, run_rtl_dot  # noqa: E402
from trinity_memory.tensorpack import Tensor, TensorPackError, decode_tensors, encode_tensors, inspect_tensorpack  # noqa: E402

CONFORMANCE = ROOT / "conformance"
CLI = ROOT / "build" / "t27" / "trinity-memory-t27"
WASM = ROOT / "build" / "t27" / "codecs.wasm"
RTL_DIR = ROOT / "build" / "t27" / "rtl"


class LabFailure(AssertionError):
    pass


def load(name):
    return json.loads((CONFORMANCE / name).read_text(encoding="utf-8"))


def sha256_file(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def tool_version(command):
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=30)
        text = (result.stdout or result.stderr).strip().splitlines()
        return text[0] if text else "unknown"
    except (OSError, subprocess.TimeoutExpired):
        return "missing"


def to_tensor(item):
    return Tensor(item["name"], tuple(item["shape"]), tuple(item["values"]), item["codec"],
                  tuple(item["scales"]), item["scale_axis"], tuple(item["axes"]))


def build_invalid_fixture(item):
    if "document" in item:
        return item["document"]
    recipe = item["recipe"]
    if "vectors" in recipe:
        base = recipe["vector"]
        return {"schema": "trinity.conformance.v1",
                "vectors": [dict(base, name=f"v{i}") for i in range(recipe["vectors"])]}
    count = recipe["weights"]
    return {"schema": "trinity.conformance.v1",
            "vectors": [{"name": "long", "weights": [recipe["weight"]] * count, "activations": [recipe["activation"]] * count,
                         "dot": recipe["weight"] * recipe["activation"] * count}]}


# ---------------------------------------------------------------- sections

def section_fixture(manifest, rtl, seed, work):
    expected = manifest["sections"]["fixture"]["expected"]
    path = work / "lab-fixture.json"
    path.write_text(manifest["fixture_text"], encoding="utf-8")
    report = run_conformance(path, rtl=rtl, seed=seed)
    if not report["passed"]:
        raise LabFailure("native conformance did not pass")
    if report["positive_checks"] != expected["positive_checks"] or report["corrupt_rejections"] != expected["corrupt_rejections"]:
        raise LabFailure(f"native counts {report['positive_checks']}/{report['corrupt_rejections']} differ from the manifest")
    if report["rtl_checks"] != (expected["rtl_checks"] if rtl else 0):
        raise LabFailure(f"native rtl checks {report['rtl_checks']} differ from the manifest")
    if report["fixture_sha256"] != sha256_file(path) or report["physical_device_tested"]:
        raise LabFailure("native report provenance mismatch")
    for item in manifest["invalid_fixtures"]:
        path.write_text(json.dumps(build_invalid_fixture(item)), encoding="utf-8")
        try:
            run_conformance(path, rtl=False, seed=seed)
        except ValueError:
            continue
        raise LabFailure(f"invalid fixture accepted: {item['id']}")
    containers = 0
    for item in manifest["fixture_containers"]:
        vector = next(v for v in manifest["fixture_document"]["vectors"] if v["name"] == item["vector"])
        data = encode_tensors([Tensor("weights", (len(vector["weights"]),), tuple(vector["weights"]), item["codec"])])
        if data.hex() != item["ttpk_hex"]:
            raise LabFailure(f"fixture container differs: {item['vector']}/{item['codec']}")
        containers += 1
    flips = 0
    with BridgeServer() as server:
        client = BridgeClient(server.url)
        for flip in manifest["fixture_corruption"]["flips"]:
            try:
                client.upload(bytes.fromhex(flip["ttpk_hex"]))
            except BridgeError as error:
                if error.code != manifest["constants"]["plan"]["corruption_rpc_error"]:
                    raise LabFailure(f"corruption {flip['index']} rejected with {error.code}")
                flips += 1
                continue
            raise LabFailure(f"corruption {flip['index']} accepted")
        if server.object_count or server.stored_bytes:
            raise LabFailure("a rejected upload allocated storage")
    return {"evidence": list(report["evidence"]), "passed": True, "rtl": rtl,
            "checks": report["positive_checks"] + report["corrupt_rejections"] + report["rtl_checks"] + len(manifest["invalid_fixtures"]) + containers + flips,
            "native": {key: report[key] for key in ("schema", "seed", "fixture_sha256", "positive_checks", "corrupt_rejections", "rtl_checks", "runtime")},
            "native_checks": report["checks"], "invalid_fixtures_rejected": len(manifest["invalid_fixtures"]),
            "fixture_containers": containers, "corruption_flips_rejected": flips}


def section_codecs(work):
    document = load("memory_types.json")
    checks = 0
    for vector in document["vectors"]:
        kind = vector["kind"]
        if kind == "group":
            payload = pack(vector["trits"], vector["codec"])
            if payload.hex() != vector["payload_hex"] or unpack(payload, len(vector["trits"]), vector["codec"]) != vector["trits"]:
                raise LabFailure(f"codec vector {vector['id']}")
            checks += 2
        elif kind == "container":
            data = encode_file(vector["trits"], vector["codec"])
            if data.hex() != vector["tmem_hex"] or decode_file(data) != vector["trits"]:
                raise LabFailure(f"container vector {vector['id']}")
            checks += 2
        elif kind == "invalid_word":
            with contextlib.suppress(CodecError):
                unpack(bytes.fromhex(vector["payload_hex"]), vector["count"], vector["codec"])
                raise LabFailure(f"invalid word accepted: {vector['id']}")
            checks += 1
        elif kind == "invalid_sparsity":
            with contextlib.suppress(CodecError):
                pack(vector["trits"], vector["codec"])
                raise LabFailure(f"invalid sparsity accepted: {vector['id']}")
            checks += 1
    wasm = {"skipped": "codecs.wasm or node missing"}
    if WASM.is_file() and shutil.which("node"):
        result = subprocess.run(["node", str(ROOT / "tests" / "spec_wasm_replay.mjs"), str(WASM), str(CONFORMANCE / "memory_types.json")],
                                capture_output=True, text=True, timeout=120)
        if result.returncode:
            raise LabFailure(f"wasm replay failed\n{result.stdout}\n{result.stderr}")
        wasm = json.loads(result.stdout.strip().splitlines()[-1])
        checks += wasm["checks"]
    return {"evidence": ["software", "wasm"], "passed": True, "checks": checks, "python_adapter_checks": checks - wasm.get("checks", 0),
            "wasm": wasm}


def section_containers(work):
    document = load("memory_tensorpack.json")
    checks = 0
    cli_checks = 0
    for vector in document["vectors"]:
        if vector["kind"] == "pack":
            tensors = [to_tensor(item) for item in vector["tensors"]]
            data = encode_tensors(tensors)
            if data.hex() != vector["ttpk_hex"] or decode_tensors(data) != tensors or inspect_tensorpack(data)["tensor_count"] != len(tensors):
                raise LabFailure(f"pack vector {vector['id']}")
            checks += 3
        elif vector["kind"] == "invalid_pack":
            data = bytes.fromhex(vector["ttpk_hex"])
            for reader in (decode_tensors, inspect_tensorpack):
                with contextlib.suppress(TensorPackError):
                    reader(data)
                    raise LabFailure(f"invalid pack accepted: {vector['id']}")
                checks += 1
        if CLI.is_file() and os.access(CLI, os.X_OK):
            path = work / f"{vector['id']}.ttpk"
            path.write_bytes(bytes.fromhex(vector["ttpk_hex"]))
            result = subprocess.run([str(CLI), "tensor-inspect", str(path)], capture_output=True, text=True, timeout=60)
            if (result.returncode == 0) != (vector["kind"] == "pack"):
                raise LabFailure(f"CLI disagrees on {vector['id']}")
            cli_checks += 1
    return {"evidence": ["software"], "passed": True, "checks": checks + cli_checks, "python_adapter_checks": checks,
            "cli_checks": cli_checks if cli_checks else "skipped: CLI not built"}


class TcpSession:
    def __init__(self, server):
        self.port = int(server.url.split(":")[-1].strip("/"))

    def send(self, body, headers=None):
        connection = HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            connection.request("POST", "/", body, headers or {"Content-Type": "application/json"})
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def raw(self, text):
        with socket.create_connection(("127.0.0.1", self.port), timeout=5) as connection:
            connection.sendall(text.replace("{port}", str(self.port)).encode("latin-1"))
            data = b""
            while chunk := connection.recv(65536):
                data += chunk
        head, _, body = data.partition(b"\r\n\r\n")
        return int(head.split(b" ", 2)[1]), json.loads(body)


@contextlib.contextmanager
def tcp_session(limits):
    with BridgeServer(**limits) as server:
        yield TcpSession(server)


def section_bridge(manifest):
    document = load("memory_bridge.json")
    run, steps, skipped = spec_bridge_replay.replay(document, tcp_session, transport=True)
    if skipped or run != len(document["vectors"]):
        raise LabFailure("bridge replay incomplete")
    lab = {}
    data = encode_file([1, -1, 0, 1])
    with BridgeServer() as server:
        handle = BridgeClient(server.url).upload(data)
        # Every request is its own TCP connection; the object outlives the client that uploaded it.
        if BridgeClient(server.url).read(handle) != data:
            raise LabFailure("object lost after the uploading connection closed")
        lab["client_disconnect_after_upload"] = True
    with BridgeServer() as server:
        try:
            BridgeClient(server.url).read(handle)
        except BridgeError as error:
            if error.code != -32004:
                raise LabFailure(f"restart returned {error.code}")
            lab["server_restart_clears_objects"] = True
        else:
            raise LabFailure("object survived a server restart")
    for name in manifest["sections"]["bridge"]["lab_checks"]:
        if not lab.get(name):
            raise LabFailure(f"lab check missing: {name}")
    return {"evidence": ["emulator", "software-loopback-http"], "passed": True, "checks": run + steps + len(lab),
            "vectors": run, "sequence_steps": steps, "lab_checks": lab}


def section_stream(work):
    document = load("memory_stream_compute.json")
    dots, storages, cycles = spec_stream_replay.replay(document, RTL_DIR, work / "traces")
    return {"evidence": ["rtl-simulation"], "passed": True, "checks": cycles, "dot_traces": dots, "storage_traces": storages,
            "compared_cycles": cycles, "simulator": "Icarus Verilog"}


def section_frames(seed):
    document = load("memory_stream_compute.json")
    results = []
    for vector in (v for v in document["vectors"] if v["kind"] == "frame"):
        expect = vector["expect"]
        if vector["acc_width"] == 32:
            observed = run_rtl_dot(vector["weights"], vector["activations"], vector["codec"], seed=seed)
            got = (observed["result"], bool(observed["error"]))
            cycles = observed["cycles"]
        else:
            packets = _packets(vector["weights"], vector["activations"], vector["codec"])
            observed = _run_packets(packets, [(expect["result"], expect["error"])], vector["codec"], seed=seed, acc_width=vector["acc_width"])
            got = (observed["results"][0]["result"], bool(observed["results"][0]["error"]))
            cycles = observed["cycles"]
        if got != (expect["result"], expect["error"]):
            raise LabFailure(f"frame {vector['id']}: {got} != expected")
        results.append({"id": vector["id"], "result": got[0], "error": got[1], "cycles": cycles})
    return {"evidence": ["rtl-simulation"], "passed": True, "checks": len(results), "frames": results}


def catalogue_coverage(manifest):
    documents = {name: load(name) for name in ("memory_types.json", "memory_bridge.json", "memory_tensorpack.json", "memory_stream_compute.json")}
    ids = {name: {vector["id"] for vector in document["vectors"]} for name, document in documents.items()}
    coverage = {}
    for family, cases in manifest["catalogue"].items():
        coverage[family] = {}
        for case, references in cases.items():
            resolved = []
            for reference in references:
                source, _, identifier = reference.partition("#")
                if source == "lab":
                    ok = identifier in manifest["sections"]["bridge"]["lab_checks"]
                elif source == "fixture_corruption":
                    ok = int(identifier) < len(manifest["fixture_corruption"]["flips"])
                else:
                    ok = identifier in ids.get(source, set())
                if not ok:
                    raise LabFailure(f"catalogue reference does not resolve: {reference}")
                resolved.append(reference)
            coverage[family][case] = resolved
    return coverage


def run_lab(rtl, seed):
    manifest = load("memory_conformance.json")
    tools_ok = all(shutil.which(tool) for tool in ("iverilog", "vvp"))
    if rtl and not tools_ok:
        raise LabFailure("Icarus Verilog is required for the RTL sections; pass --no-rtl to skip them explicitly")
    timing = {}
    sections = {}
    with tempfile.TemporaryDirectory(prefix="trinity-lab-") as temporary:
        work = Path(temporary)
        plan = [("fixture", lambda: section_fixture(manifest, rtl, seed, work)), ("codecs", lambda: section_codecs(work)),
                ("containers", lambda: section_containers(work)), ("bridge", lambda: section_bridge(manifest))]
        if rtl:
            plan += [("stream", lambda: section_stream(work)), ("frames", lambda: section_frames(seed))]
        else:
            sections["stream"] = {"skipped": "--no-rtl", "passed": False}
            sections["frames"] = {"skipped": "--no-rtl", "passed": False}
        for name, runner in plan:
            started = time.monotonic()
            sections[name] = runner()
            timing[name] = round(time.monotonic() - started, 3)
    sources = {}
    for path in sorted(list((ROOT / "specs" / "memory").glob("*.t27")) + list(CONFORMANCE.glob("*.json")) + [ROOT / "examples" / "conformance.json", ROOT / "native" / "compiler.lock"]):
        sources[str(path.relative_to(ROOT))] = sha256_file(path)
    tools = {"python": platform.python_version(), "platform": platform.platform(), "compiler_lock": (ROOT / "native" / "compiler.lock").read_text().strip(),
             "iverilog": tool_version(["iverilog", "-V"]), "node": tool_version(["node", "--version"]),
             "native_revision": (ROOT / "build" / "t27" / "compiler.revision").read_text().strip() if (ROOT / "build" / "t27" / "compiler.revision").is_file() else "unknown"}
    report = {"schema": "trinity.conformance-lab.v1", "passed": all(section.get("passed") for section in sections.values()),
              "seed": seed, "rtl": rtl, "sections": sections, "catalogue": catalogue_coverage(manifest),
              "sources": sources, "tools": tools, "timing": timing, "physical_device_tested": False,
              "evidence": sorted({label for section in sections.values() for label in section.get("evidence", [])})}
    return report


def strip_timing(report):
    return {key: value for key, value in report.items() if key != "timing"}


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--output", type=Path, default=ROOT / "build" / "conformance-lab.json")
    parser.add_argument("--seed", type=int, default=27)
    parser.add_argument("--no-rtl", action="store_true", help="skip the RTL sections explicitly (the report will not pass)")
    parser.add_argument("--repeat", type=int, default=1, help="run N times and require identical reports except timing")
    args = parser.parse_args()
    reports = [run_lab(not args.no_rtl, args.seed) for _ in range(max(1, args.repeat))]
    first = strip_timing(reports[0])
    for index, other in enumerate(reports[1:], start=2):
        if strip_timing(other) != first:
            raise SystemExit(f"run {index} differs from run 1 outside the timing fields")
    report = reports[-1]
    report["deterministic"] = {"runs": len(reports), "identical_except_timing": len(reports) > 1}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    counts = {name: section.get("checks", 0) for name, section in report["sections"].items()}
    print(f"{'PASS' if report['passed'] else 'INCOMPLETE'} conformance lab: {counts} evidence={report['evidence']} runs={len(reports)} -> {args.output}")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
