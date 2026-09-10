#!/usr/bin/env python3
"""Actual executable/HTTP parity checks for the native t27 CLI migration."""
from __future__ import annotations
import argparse
from dataclasses import asdict
import json
from pathlib import Path
import random
import re
import select
import signal
import subprocess
import sys
import tempfile


def run(binary: Path, root: Path) -> dict:
    sys.path.insert(0, str(root / "tests/reference"))
    from trinity_memory_reference.bridge import BridgeClient, BridgeServer
    from trinity_memory_reference.codecs import CODECS, LANE_ENCODE, pack
    from trinity_memory_reference.container import encode_file, inspect_file
    from trinity_memory_reference.tensorpack import Tensor, encode_tensors, inspect_tensorpack
    rng = random.Random(27)
    checks = 0

    def cli(*args, success=True):
        nonlocal checks
        result = subprocess.run([str(binary), *map(str, args)], capture_output=True, text=True, timeout=30)
        checks += 1
        assert (result.returncode == 0) == success, (args, result.returncode, result.stdout, result.stderr)
        return result

    with tempfile.TemporaryDirectory(prefix="trinity-native-cli-") as temporary:
        work = Path(temporary)
        source, packed, restored = work / "source.json", work / "file.tmem", work / "restored.json"
        for codec in CODECS.values():
            for count in (0, 1, 4, 5, 17, 22, 129):
                values = [rng.choice((-1, 0, 1)) for _ in range(count)]
                if codec.max_nonzero is not None:
                    values = [value if i % codec.group_size < codec.max_nonzero else 0 for i, value in enumerate(values)]
                source.write_text(json.dumps(values))
                actual = cli("pack", "--codec", codec.name, source, packed)
                expected = encode_file(values, codec.name)
                assert packed.read_bytes() == expected
                assert json.loads(actual.stdout) == inspect_file(expected)
                assert json.loads(cli("inspect", packed).stdout) == inspect_file(expected)
                cli("unpack", packed, restored)
                assert json.loads(restored.read_text()) == values
        for codec in ("dense5", "sparse41", "baseline5"):
            for count in (1, 4, 5, 6, 17, 63):
                n = 4 if codec == "sparse41" else 5
                values = [rng.choice((-1, 0, 1)) for _ in range(count)]
                if codec == "sparse41":
                    values = [value if i % 4 == 0 else 0 for i, value in enumerate(values)]
                source.write_text(json.dumps(values))
                cli("export-rtl", source, restored, f"--codec={codec}")
                words = []
                for start in range(0, len(values), n):
                    group = values[start:start + n] + [0] * max(0, n - len(values[start:start + n]))
                    if codec == "baseline5":
                        word = sum(LANE_ENCODE[v] << (2 * i) for i, v in enumerate(group))
                        words.append(f"{word:03x}")
                    else:
                        word = pack(group, codec)[0]
                        words.append(format(word, "01x" if n == 4 else "02x"))
                assert restored.read_text() == "\n".join(words) + "\n"
        source.write_text("[]")
        cli("export-rtl", source, restored, success=False)
        for value in ("[true]", "[1.0]", "[2]", "[1,]", "{\"x\":1}", "[NaN]"):
            source.write_text(value)
            packed.write_bytes(b"preserve-existing")
            cli("pack", source, packed, success=False)
            assert packed.read_bytes() == b"preserve-existing"
        tensors = [Tensor("matrix", (2, 3), (1, -1, 0, -1, 1, 1), scales=(0.5, 2.0), scale_axis=0, axes=("row", "col")),
                   Tensor("vector ☃", (3,), (-1, 0, 1), codec="dense22", scales=(1e-8,)),
                   Tensor("scalar", (), (0,), scales=(1e20,))]
        for items in ([], tensors):
            source.write_text(json.dumps([asdict(tensor) for tensor in items]))
            actual = cli("tensor-pack", source, packed)
            expected = encode_tensors(items)
            assert packed.read_bytes() == expected
            assert json.loads(actual.stdout) == inspect_tensorpack(expected)
            assert json.loads(cli("tensor-inspect", packed).stdout) == inspect_tensorpack(expected)
            cli("tensor-unpack", packed, restored)
            assert json.loads(restored.read_text()) == json.loads(json.dumps([asdict(tensor) for tensor in items]))
        for text in ('{}', '[{"name":"bad"}]', '[{"name":"v","shape":[1],"values":[true]}]'):
            source.write_text(text)
            packed.write_bytes(b"preserve-existing")
            cli("tensor-pack", source, packed, success=False)
            assert packed.read_bytes() == b"preserve-existing"

        data = encode_tensors(tensors)
        packed.write_bytes(data)
        source.write_text("[-128,127,-128]")

        def check_remote(url):
            client = BridgeClient(url)
            uploaded = json.loads(cli("upload", packed, f"--url={url}").stdout)
            handle = uploaded["handle"]
            assert uploaded["info"]["sha256"] == client.info(handle)["sha256"]
            assert client.read(handle) == data
            result = json.loads(cli("dot", handle, "matrix", source, "--url", url).stdout)
            assert result == client.dot(handle, "matrix", [-128, 127, -128])
            assert result["accumulators"] == [-255, 127]
            cli("download", handle, restored, "--url", url)
            assert restored.read_bytes() == data
            cli("dot", handle, "missing", source, "--url", url, success=False)
            cli("download", "f" * 32, restored, "--url", url, success=False)
            client.delete(handle)

        with BridgeServer(port=0) as reference:
            check_remote(reference.url)
        server = subprocess.Popen([str(binary), "serve", "--port=0"], stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, text=True)
        try:
            ready, _, _ = select.select([server.stdout], [], [], 10)
            assert ready, "native serve did not announce readiness"
            line = server.stdout.readline()
            found = re.search(r"http://127\.0\.0\.1:[0-9]+/", line)
            assert found, (line, server.poll())
            check_remote(found.group())
        finally:
            server.send_signal(signal.SIGTERM)
            stdout, stderr = server.communicate(timeout=10)
            assert server.returncode == 0, (server.returncode, stdout, stderr)
        for url in ("https://127.0.0.1:8787", "http://example.com:8787", "http://127.0.0.1", "http://user@127.0.0.1:8787", "http://127.0.0.1:8787/?x=1"):
            cli("upload", packed, "--url", url, success=False)
        cli("serve", "--port", "65536", success=False)
    return {"evidence": "native-t27-cli-and-real-loopback-http", "executable_checks": checks,
            "native_server_tested": True, "python_server_tested": True,
            "file_bytes_and_metadata_parity": True, "all_codecs": list(CODECS)}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--source-root", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = run(args.binary.resolve(), args.source_root)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, sort_keys=True))
