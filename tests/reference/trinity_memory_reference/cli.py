"""CLI with JSON inputs and explicit output paths; no model downloads."""

import argparse
import json
from pathlib import Path
import sys

from .benchmark import run_benchmark
from .codecs import CODECS, CodecError, LANE_ENCODE, pack, validate_trits
from .container import decode_file, encode_file, inspect_file


def _read_json(path: Path):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON key: {key}")
            result[key] = value
        return result

    def constant(value):
        raise ValueError(f"nonfinite JSON number: {value}")

    return json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=unique, parse_constant=constant)


def _read_values(path: Path) -> list[int]:
    value = _read_json(path)
    if not isinstance(value, list):
        raise CodecError("JSON input must be an array of integer trits")
    return validate_trits(value)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Trinity ternary memory reference experiments")
    commands = parser.add_subparsers(dest="command", required=True)
    encode = commands.add_parser("pack", help="encode JSON integer trits into TMEM v1")
    encode.add_argument("input", type=Path)
    encode.add_argument("output", type=Path)
    encode.add_argument("--codec", choices=CODECS, default="dense5")
    decode = commands.add_parser("unpack", help="validate and decode a TMEM file to JSON")
    decode.add_argument("input", type=Path)
    decode.add_argument("output", type=Path)
    inspect = commands.add_parser("inspect", help="validate a TMEM file and print its metadata")
    inspect.add_argument("input", type=Path)
    bench = commands.add_parser("benchmark", help="run synthetic size/timing/round-trip experiments")
    bench.add_argument("--count", type=int, default=65536)
    bench.add_argument("--repeats", type=int, default=3)
    bench.add_argument("--seed", type=int, default=27)
    bench.add_argument("--output", type=Path, default=Path("reports/benchmark.json"))
    rtl = commands.add_parser("export-rtl", help="write raw hex words for RTL $readmemh")
    rtl.add_argument("input", type=Path)
    rtl.add_argument("output", type=Path)
    rtl.add_argument("--codec", choices=("dense5", "sparse41", "baseline5"), default="dense5")
    tensors = commands.add_parser("tensor-pack", help="encode JSON named ternary tensors into TensorPack v1")
    tensors.add_argument("input", type=Path)
    tensors.add_argument("output", type=Path)
    tensor_read = commands.add_parser("tensor-unpack", help="validate TensorPack and export tensors as JSON")
    tensor_read.add_argument("input", type=Path)
    tensor_read.add_argument("output", type=Path)
    tensor_info = commands.add_parser("tensor-inspect", help="validate TensorPack and show metadata")
    tensor_info.add_argument("input", type=Path)
    serve = commands.add_parser("serve", help="run the loopback-only Bridge memory emulator")
    serve.add_argument("--port", type=int, default=8787)
    transfer = commands.add_parser("upload", help="upload TMEM/TensorPack to a running Bridge")
    transfer.add_argument("input", type=Path)
    transfer.add_argument("--url", default="http://127.0.0.1:8787")
    read = commands.add_parser("download", help="read a stored container from Bridge")
    read.add_argument("handle")
    read.add_argument("output", type=Path)
    read.add_argument("--url", default="http://127.0.0.1:8787")
    dot = commands.add_parser("dot", help="compute via the Bridge software emulator")
    dot.add_argument("handle")
    dot.add_argument("tensor")
    dot.add_argument("input", type=Path, help="JSON integer activation array (-128..127)")
    dot.add_argument("--url", default="http://127.0.0.1:8787")
    edge = commands.add_parser("edge-demo", help="run the signal classifier over real loopback HTTP")
    edge.add_argument("--rtl", action="store_true", help="also check Icarus RTL (tools required)")
    edge.add_argument("--output", type=Path, default=Path("build/edge-report.json"))
    edge.add_argument("--html", type=Path, default=Path("build/edge-report.html"))
    conformance = commands.add_parser("conformance", help="check golden vectors across codecs, Bridge and optional RTL")
    conformance.add_argument("--vectors", type=Path, default=Path("examples/conformance.json"))
    conformance.add_argument("--rtl", action="store_true")
    conformance.add_argument("--output", type=Path, default=Path("build/conformance-report.json"))
    args = parser.parse_args(argv)
    try:
        if args.command == "pack":
            data = encode_file(_read_values(args.input), args.codec)
            args.output.write_bytes(data)
            print(json.dumps(inspect_file(data), indent=2))
        elif args.command == "unpack":
            values = decode_file(args.input.read_bytes())
            args.output.write_text(json.dumps(values) + "\n", encoding="utf-8")
            print(f"Decoded {len(values)} trits to {args.output}")
        elif args.command == "inspect":
            print(json.dumps(inspect_file(args.input.read_bytes()), indent=2))
        elif args.command == "benchmark":
            report = run_benchmark(args.count, args.repeats, args.seed)
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
            print(f"Verified {sum(len(d['results']) for d in report['datasets'])} round trips; "
                  f"report: {args.output}")
        elif args.command == "export-rtl":
            values = _read_values(args.input)
            if not values:
                raise CodecError("RTL memory export requires at least one trit")
            words = []
            n = 4 if args.codec == "sparse41" else 5
            for start in range(0, len(values), n):
                group = values[start:start + n]
                group += [0] * (n - len(group))
                if args.codec == "baseline5":
                    word = sum(LANE_ENCODE[t] << (2 * i) for i, t in enumerate(group))
                    words.append(f"{word:03x}")
                else:
                    word = pack(group, args.codec)[0]
                    words.append(format(word, "01x" if n == 4 else "02x"))
            args.output.write_text("\n".join(words) + "\n", encoding="ascii")
            print(f"Exported {len(words)} words; logical count={len(values)}, "
                  f"lanes={n}; set the RTL weight count separately")
        elif args.command in ("tensor-pack", "tensor-unpack", "tensor-inspect"):
            from dataclasses import asdict
            from .tensorpack import Tensor, decode_tensors, encode_tensors, inspect_tensorpack
            if args.command == "tensor-pack":
                document = _read_json(args.input)
                if not isinstance(document, list):
                    raise ValueError("tensor input must be an array of tensor objects")
                items = []
                for item in document:
                    if not isinstance(item, dict):
                        raise ValueError("tensor entries must be objects")
                    values = dict(item)
                    for field in ("shape", "values", "scales", "axes"):
                        if field in values:
                            if not isinstance(values[field], list):
                                raise ValueError(f"{field} must be a JSON array")
                            values[field] = tuple(values[field])
                    items.append(Tensor(**values))
                data = encode_tensors(items)
                args.output.write_bytes(data)
                print(json.dumps(inspect_tensorpack(data), indent=2))
            elif args.command == "tensor-unpack":
                tensors = decode_tensors(args.input.read_bytes())
                args.output.write_text(json.dumps([asdict(t) for t in tensors], indent=2) + "\n", encoding="utf-8")
            else:
                print(json.dumps(inspect_tensorpack(args.input.read_bytes()), indent=2))
        elif args.command == "serve":
            import threading
            from .bridge import BridgeServer
            with BridgeServer(port=args.port) as server:
                print(f"Trinity Bridge EMULATOR: {server.url} (Ctrl-C to stop)", flush=True)
                try:
                    threading.Event().wait()
                except KeyboardInterrupt:
                    pass
        elif args.command in ("upload", "download", "dot"):
            from .bridge import BridgeClient
            client = BridgeClient(args.url)
            if args.command == "upload":
                handle = client.upload(args.input.read_bytes())
                print(json.dumps({"handle": handle, "info": client.info(handle)}, indent=2))
            elif args.command == "download":
                args.output.write_bytes(client.read(args.handle))
            else:
                samples = _read_json(args.input)
                print(json.dumps(client.dot(args.handle, args.tensor, samples), indent=2))
        elif args.command == "edge-demo":
            from .edge import render_edge_report, run_edge_demo
            report = run_edge_demo(rtl=args.rtl)
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
            render_edge_report(report, args.html)
            print(f"PASS: {report['fixture_count']} fixtures x 2 codecs; {args.output}; {args.html}")
        elif args.command == "conformance":
            from .conformance import run_conformance
            report = run_conformance(args.vectors, rtl=args.rtl)
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
            print(f"PASS: {report['positive_checks']} positive checks, "
                  f"{report['corrupt_rejections']} corrupt rejections, {report['rtl_checks']} RTL checks")
    except (OSError, ValueError, TypeError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
