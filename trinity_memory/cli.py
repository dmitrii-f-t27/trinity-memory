"""CLI with JSON inputs and explicit output paths; no model downloads."""

import argparse
import json
from pathlib import Path
import sys

from .benchmark import run_benchmark
from .codecs import CODECS, CodecError, LANE_ENCODE, pack, validate_trits
from .container import decode_file, encode_file, inspect_file


def _read_values(path: Path) -> list[int]:
    value = json.loads(path.read_text(encoding="utf-8"))
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
    except (OSError, ValueError, TypeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
